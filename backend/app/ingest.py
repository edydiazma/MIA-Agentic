"""Procesa los webhooks de WhatsApp: mensajes, estados (con facturación), opt-outs de marketing y
alertas de plantillas, calidad del número y cuenta. El payload crudo queda en inbound_events."""

import logging

from sqlalchemy import select

from app import storage, templates
from app.agent import schedule_reply
from app.ai.transcribe import transcribe
from app.automations import on_inbound
from app.db import SessionLocal, set_actor
from app.models import CampaignRecipient, Channel, Contact, Message, MessageWaId, utcnow
from app.realtime import hub
from app.service import (
    create_alert,
    get_or_create_contact,
    get_or_create_conversation,
    message_out,
    record_message,
    wa_client,
)

log = logging.getLogger(__name__)

MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")
MESSAGE_TYPES = {"text", "image", "audio", "video", "document", "sticker", "location", "interactive", "contacts"}
OPT_OUT_CODE = 131050  # el usuario pidió no recibir marketing
STATUS_ORDER = ["pending", "sent", "delivered", "read"]


def _text_of(m: dict) -> str | None:
    t = m.get("type")
    if t == "text":
        return m["text"]["body"]
    if t in ("image", "video", "document"):
        return m[t].get("caption")
    if t == "location":
        loc = m["location"]
        parts = [loc.get("name"), loc.get("address"), f"({loc.get('latitude')}, {loc.get('longitude')})"]
        return "[Ubicación] " + " ".join(p for p in parts if p)
    if t == "interactive":
        inter = m["interactive"]
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title")
    if t == "button":
        return m["button"].get("text")
    if t == "contacts":
        names = [c.get("name", {}).get("formatted_name", "") for c in m["contacts"]]
        phones = [p.get("phone", "") for c in m["contacts"] for p in c.get("phones", [])]
        return "[Contacto compartido] " + ", ".join(names + phones)
    return None


def _msg_type(t: str) -> str:
    if t == "button":
        return "interactive"
    return t if t in MESSAGE_TYPES else "unsupported"


class WebhookProcessingError(Exception):
    """Algún cambio del webhook falló; el router lo registra en inbound_events.error para reprocesar."""


async def process_webhook(payload: dict) -> None:
    """Procesa cada cambio por separado (uno que falle no bloquea a los demás).
    El payload crudo lo guarda el router en inbound_events antes de llamar aquí."""
    errors = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field", "messages")
            value = change.get("value", {})
            try:
                if field == "calls":  # WhatsApp Business Calling API (app/voice/calls.py)
                    from app.voice.calls import handle_calls_webhook

                    await handle_calls_webhook(value)
                elif field == "messages":
                    phone_number_id = value.get("metadata", {}).get("phone_number_id")
                    for status in value.get("statuses", []):
                        await _handle_status(status, phone_number_id)
                    profiles = {c["wa_id"]: c.get("profile", {}).get("name") for c in value.get("contacts", [])}
                    for m in value.get("messages", []):
                        await _handle_message(phone_number_id, m, profiles.get(m["from"]))
                else:
                    await _handle_account_event(entry.get("id"), field, value)
            except Exception as e:
                log.exception("Error procesando el webhook (%s)", field)
                errors.append(f"{field}: {type(e).__name__}: {e}")
    if errors:
        raise WebhookProcessingError("; ".join(errors)[:2000])


# --- Estados de mensajes salientes ----------------------------------------------
async def _handle_status(status: dict, phone_number_id: str | None = None) -> None:
    async with SessionLocal() as session:
        new = status["status"]
        errors = status.get("errors", [])
        error_text = "; ".join(f"{e.get('code')}: {e.get('title', '')}" for e in errors) or None
        error_code = next((e.get("code") for e in errors if isinstance(e.get("code"), int)), None)
        now = utcnow()

        ref = await session.get(MessageWaId, status["id"])
        msg = None
        if ref:
            msg = await session.get(Message, (ref.message_id, ref.message_created_at))
        # Multiempresa: la empresa sale del mensaje o del número que recibe; nunca de la configuración
        org = ref.organization_id if ref else await session.scalar(
            select(Channel.organization_id).where(Channel.phone_number_id == phone_number_id)) if phone_number_id else None

        recipient = await session.scalar(select(CampaignRecipient).where(CampaignRecipient.wa_message_id == status["id"]))
        if recipient:
            if new == "failed":
                recipient.status, recipient.error, recipient.error_code = "failed", error_text, error_code
                recipient.failed_at = now
            elif new in STATUS_ORDER and STATUS_ORDER.index(new) > STATUS_ORDER.index(
                    recipient.status if recipient.status in STATUS_ORDER else "pending"):
                recipient.status = new
            if new == "delivered":
                recipient.delivered_at = recipient.delivered_at or now
            elif new == "read":
                recipient.read_at = recipient.read_at or now
                recipient.delivered_at = recipient.delivered_at or now

        if org and any(e.get("code") == OPT_OUT_CODE for e in errors):
            contact = await session.scalar(select(Contact).where(
                Contact.organization_id == org, Contact.wa_id == status.get("recipient_id")))
            if contact and not contact.marketing_opt_out:
                contact.marketing_opt_out, contact.opt_out_at = True, now
                await create_alert(
                    session, org, severity="warning", layer="contact", source="meta",
                    title="Un contacto pidió dejar de recibir marketing",
                    description="Solo puedes enviarle mensajes de utilidad y autenticación; "
                    "los mensajes de marketing generarán un error.", ref=contact.wa_id)

        if msg:
            pricing = status.get("pricing") or {}
            if pricing:
                msg.pricing_category = pricing.get("category")
                msg.billable = pricing.get("billable")
            if new == "failed":
                msg.status, msg.error, msg.error_code = "failed", error_text, error_code
            elif new in STATUS_ORDER and (
                    msg.status not in STATUS_ORDER or STATUS_ORDER.index(new) > STATUS_ORDER.index(msg.status)):
                msg.status = new
        await session.commit()
        if msg:
            await hub.broadcast("message.status", message_out(msg), msg.organization_id)


# --- Alertas de la cuenta (plantillas, calidad, cuenta) -------------------------
async def _org_for_waba(session, waba_id: str | None) -> int | None:
    """Empresa dueña de la cuenta de WhatsApp (None si no es de ninguna: el evento se ignora)."""
    if not waba_id:
        return None
    return await session.scalar(select(Channel.organization_id).where(Channel.waba_id == waba_id).limit(1))


async def _handle_account_event(waba_id: str | None, field: str, v: dict) -> None:
    async with SessionLocal() as session:
        org = await _org_for_waba(session, waba_id)
        if org is None:
            log.warning("Evento %s de una cuenta de WhatsApp no registrada (%s): se ignora", field, waba_id)
            return
        if field == "message_template_status_update":
            await templates.invalidate(session, org)
            await session.commit()
            event = v.get("event")
            if event in ("REJECTED", "PAUSED", "DISABLED", "FLAGGED", "PENDING_DELETION"):
                await create_alert(
                    session, org, severity="critical" if event in ("DISABLED", "REJECTED") else "warning",
                    layer="template", source="meta",
                    title=f"Plantilla «{v.get('message_template_name')}» {event.lower()}",
                    description=v.get("reason") or (v.get("other_info") or {}).get("description"),
                    ref=v.get("message_template_name"))
        elif field == "message_template_quality_update":
            new = v.get("new_quality_score")
            if new in ("RED", "YELLOW"):
                await create_alert(
                    session, org, severity="critical" if new == "RED" else "warning", layer="template", source="meta",
                    title=f"Calidad de la plantilla «{v.get('message_template_name')}» bajó a {new}",
                    description=f"Antes: {v.get('previous_quality_score')}", ref=v.get("message_template_name"))
        elif field == "phone_number_quality_update":
            event = v.get("event")
            if event in ("FLAGGED", "DOWNGRADE"):
                await create_alert(
                    session, org, severity="critical", layer="phone", source="meta",
                    title=f"Número {v.get('display_phone_number')}: {event.lower()}",
                    description=f"Límite actual: {v.get('current_limit')}", ref=v.get("display_phone_number"))
        elif field in ("account_update", "account_alerts"):
            info = v.get("alert_info") or {}
            await create_alert(
                session, org, severity="critical" if v.get("ban_info") else "warning", layer="account",
                source="meta", title=info.get("alert_type") or f"Cuenta: {v.get('event', field)}",
                description=info.get("alert_description") or str(v)[:500], ref=waba_id)


# --- Mensajes entrantes ---------------------------------------------------------
async def _handle_message(phone_number_id: str, m: dict, profile_name: str | None) -> None:
    if m.get("type") in ("reaction", "system", "ephemeral"):
        return
    async with SessionLocal() as session:
        channel = await session.scalar(select(Channel).where(Channel.phone_number_id == phone_number_id))
        if not channel:
            log.warning("Mensaje para un número no registrado: %s", phone_number_id)
            return
        if await session.get(MessageWaId, m["id"]):
            return  # Meta reintenta webhooks: deduplicamos
        org = channel.organization_id
        await set_actor(session, "contact")

        contact, is_new = await get_or_create_contact(session, org, m["from"], profile_name)
        if contact.blocked:
            await session.commit()
            return  # cliente bloqueado: se ignora
        contact.last_seen_at = utcnow()

        conv = await get_or_create_conversation(session, channel, contact)
        ref = m.get("referral")
        if ref:  # Click to WhatsApp (anuncio o publicación de Meta)
            conv.ad_source_type = ref.get("source_type")
            conv.ad_source_id = ref.get("source_id")
            conv.ad_headline = (ref.get("headline") or ref.get("body") or "")[:500] or None
            conv.ad_source_url = ref.get("source_url")
            conv.ad_ctwa_clid = ref.get("ctwa_clid")

        raw_type = m.get("type", "unsupported")
        msg = Message(direction="in", sender_type="contact", type=_msg_type(raw_type), text=_text_of(m),
                      wa_message_id=m["id"], status="received")
        client = await wa_client(session, channel)
        if raw_type in MEDIA_TYPES:
            media = m[raw_type]
            try:
                data, mime = await client.download_media(media["id"])
                mime = mime.split(";")[0]
                msg.media_path = await storage.upload(
                    storage.new_path(storage.MEDIA_BUCKET, org, f"conv/{conv.id}", mime), data, mime)
                msg.media_mime, msg.media_size = mime, len(data)
                msg.media_filename = media.get("filename")
                if raw_type == "audio":
                    msg.transcript = await transcribe(data, mime)
            except Exception as e:
                log.exception("No se pudo descargar el medio %s", media.get("id"))
                msg.error = f"No se pudo descargar el archivo: {e}"[:2000]

        await record_message(session, conv, msg)
        from app.attribution import on_inbound as attribute  # ref_code web / Click to WA (app/attribution.py)

        await attribute(session, conv, msg, m)
        from app.flows.engine import handle_inbound as flows_inbound

        # Orden: flujos (incluida una espera de respuesta) → automatizaciones → bot de IA
        handled = await flows_inbound(session, conv, msg) or await on_inbound(session, conv, msg, is_new)
        from app.classifier import maybe_periodic  # import diferido: classifier depende de service

        await maybe_periodic(session, conv)
        await session.refresh(conv)
        status, conv_id = conv.status, conv.id

    try:
        await client.mark_read(m["id"])
    except Exception:
        log.debug("mark_read falló", exc_info=True)

    if status == "bot" and not handled:
        schedule_reply(conv_id)
