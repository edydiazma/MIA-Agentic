"""Procesa los webhooks de Meta: WhatsApp (mensajes, estados con facturación, opt-outs de marketing y alertas
de plantillas, calidad del número y cuenta), Messenger (object "page") e Instagram (object "instagram").
El payload crudo queda en inbound_events. Todos los canales comparten el mismo flujo después de guardar el
mensaje (after_inbound): atribución → flujos → automatizaciones → clasificación → IA."""

import logging

from sqlalchemy import select

from app import storage, templates
from app.agent import schedule_reply
from app.ai.transcribe import transcribe
from app.automations import on_inbound
from app.db import SessionLocal, set_actor
from app.models import CampaignRecipient, Channel, Contact, ContactIdentity, Conversation, Message, MessageWaId, utcnow
from app.realtime import hub
from app.service import (
    create_alert,
    get_or_create_contact,
    get_or_create_contact_by_identity,
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
    if t == "order":
        items = (m.get("order") or {}).get("product_items") or []
        lines = [f"{i.get('quantity', 1)} × {i.get('product_retailer_id')}" for i in items]
        return "[Pedido] " + ", ".join(lines) + (f" — {m['order']['text']}" if (m.get("order") or {}).get("text") else "")
    if t == "contacts":
        names = [c.get("name", {}).get("formatted_name", "") for c in m["contacts"]]
        phones = [p.get("phone", "") for c in m["contacts"] for p in c.get("phones", [])]
        return "[Contacto compartido] " + ", ".join(names + phones)
    return None


def _msg_type(t: str) -> str:
    if t == "button":
        return "interactive"
    if t == "order":
        return "product"
    return t if t in MESSAGE_TYPES else "unsupported"


class WebhookProcessingError(Exception):
    """Algún cambio del webhook falló; el router lo registra en inbound_events.error para reprocesar."""


async def process_webhook(payload: dict) -> None:
    """Procesa cada cambio por separado (uno que falle no bloquea a los demás).
    El payload crudo lo guarda el router en inbound_events antes de llamar aquí."""
    if payload.get("object") in ("page", "instagram"):
        await process_meta_messaging(payload)
        return
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
                    contacts = value.get("contacts", [])
                    for m in value.get("messages", []):
                        await _handle_message(phone_number_id, m, _sender(contacts, m))
                elif field == "user_id_update":  # el cliente cambió de número: BSUID nuevo (app/identity.py)
                    await _handle_user_id_update(value)
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
            who = (Contact.wa_bsuid == status["recipient_user_id"]) if status.get("recipient_user_id") \
                else (Contact.wa_id == status.get("recipient_id"))
            contact = await session.scalar(select(Contact).where(Contact.organization_id == org, who))
            if contact and not contact.marketing_opt_out:
                contact.marketing_opt_out, contact.opt_out_at = True, now
                await create_alert(
                    session, org, severity="warning", layer="contact", source="meta",
                    title="Un contacto pidió dejar de recibir marketing",
                    description="Solo puedes enviarle mensajes de utilidad y autenticación; "
                    "los mensajes de marketing generarán un error.", ref=contact.wa_id or contact.wa_bsuid)

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
        if msg is not None and msg.journey_id:  # journeys: entregado / leído / fallido (§21.1)
            try:
                from app.journeys.hooks import on_status as journey_status

                await journey_status(session, msg, new)
            except Exception:  # noqa: BLE001 — nunca debe impedir procesar el estado
                log.exception("Journey: no se pudo registrar el estado del mensaje %s", msg.id)
                await session.rollback()


# --- Alertas de la cuenta (plantillas, calidad, cuenta) -------------------------
async def _org_for_waba(session, waba_id: str | None) -> int | None:
    """Empresa dueña de la cuenta de WhatsApp (None si no es de ninguna: el evento se ignora)."""
    if not waba_id:
        return None
    # Determinista: el canal más antiguo de esa WABA (una WABA pertenece a una sola empresa)
    return await session.scalar(select(Channel.organization_id).where(Channel.waba_id == waba_id)
                                .order_by(Channel.id).limit(1))


async def _handle_account_event(waba_id: str | None, field: str, v: dict) -> None:
    async with SessionLocal() as session:
        org = await _org_for_waba(session, waba_id)
        if org is None:
            log.warning("Evento %s de una cuenta de WhatsApp no registrada (%s): se ignora", field, waba_id)
            return
        if field == "message_template_status_update":
            await templates.invalidate(session, org)
            from app.onboarding.meta import on_template_status  # estado y motivo de rechazo al instante

            await on_template_status(session, org, v)
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
def _sender(contacts: list[dict], m: dict) -> dict:
    """Quién escribe: teléfono (puede faltar), BSUID (user_id), BSUID padre, @usuario y nombre del perfil."""
    phone, bsuid = m.get("from"), m.get("from_user_id")
    match = next((c for c in contacts if (bsuid and c.get("user_id") == bsuid) or (phone and c.get("wa_id") == phone)),
                 contacts[0] if len(contacts) == 1 else {})
    profile = match.get("profile") or {}
    return {"phone": phone or match.get("wa_id"), "bsuid": bsuid or match.get("user_id"),
            "parent_bsuid": m.get("from_parent_user_id") or match.get("parent_user_id"),
            "username": profile.get("username"), "name": profile.get("name")}


async def _handle_user_id_update(value: dict) -> None:
    from app.identity import apply_user_id_update

    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
    async with SessionLocal() as session:
        org = await session.scalar(select(Channel.organization_id).where(Channel.phone_number_id == phone_number_id))
        if not org:
            log.warning("user_id_update para un número no registrado: %s", phone_number_id)
            return
        for update in value.get("user_id_update", []):
            await apply_user_id_update(session, org, update)
        await session.commit()


async def _handle_message(phone_number_id: str, m: dict, sender: dict | str | None) -> None:
    if not isinstance(sender, dict):  # compatibilidad: antes se pasaba solo el nombre del perfil
        sender = {"phone": m.get("from"), "name": sender}
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

        contact, is_new = await get_or_create_contact(session, org, sender.get("phone"), sender.get("name"),
                                                      bsuid=sender.get("bsuid"), username=sender.get("username"),
                                                      parent_bsuid=sender.get("parent_bsuid"))
        if contact.blocked:
            await session.commit()
            return  # cliente bloqueado: se ignora
        contact.last_seen_at = utcnow()
        channel.last_inbound_at = utcnow()
        try:  # respuesta al mensaje de prueba del asistente de onboarding (valida que los webhooks llegan)
            from app.onboarding.hooks import on_whatsapp_inbound

            if contact.wa_id:
                await on_whatsapp_inbound(session, channel, contact.wa_id)
        except Exception:
            log.debug("Gancho de onboarding falló", exc_info=True)

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
        interactive = m.get("interactive") or {}
        if raw_type == "interactive" and interactive.get("type") == "call_permission_reply":
            try:  # el cliente aceptó / rechazó recibir llamadas de la empresa (§19.1)
                from app.voice.outbound import on_permission_reply

                await on_permission_reply(session, channel, contact, conv, interactive.get("call_permission_reply") or {})
            except Exception:  # noqa: BLE001 — nunca impide procesar el mensaje
                log.exception("No se pudo registrar el permiso de llamada de la conversación %s", conv.id)
                await session.rollback()
        if raw_type == "order":  # pedido del catálogo: productos comprados (Cliente 360)
            from app.interaction_products import record_order

            await record_order(session, conv, m.get("order") or {}, msg.id)
            await session.commit()
        status, conv_id, handled = await after_inbound(session, conv, msg, m, is_new)

    try:
        await client.mark_read(m["id"])
    except Exception:
        log.debug("mark_read falló", exc_info=True)

    if status == "bot" and not handled:
        schedule_reply(conv_id)


async def after_inbound(session, conv: Conversation, msg: Message, raw: dict, is_new: bool,
                        provider: str = "whatsapp_cloud") -> tuple[str, int, bool]:
    """Mismo flujo para todos los canales una vez guardado el mensaje del cliente.
    Devuelve (estado, id de la conversación, si un flujo o automatización ya respondió)."""
    from app.attribution import on_inbound as attribute  # anuncio, ref web, mensaje disparador
    from app.classifier import maybe_periodic  # import diferido: classifier depende de service
    from app.flows.engine import handle_inbound as flows_inbound

    link = await attribute(session, conv, msg, raw, provider)
    from app.golden.hooks import on_inbound_media  # registro maestro: lee cédulas, tarjetas de propiedad, SOAT…

    await on_inbound_media(session, conv, msg)
    try:  # agente avanzado: reinicia la recuperación, reactivación por tipificación y reglas por fuente (§17)
        from app.agent_config import on_inbound as agent_config_inbound

        await agent_config_inbound(session, conv, msg)
    except Exception:  # noqa: BLE001 — nunca debe impedir que el mensaje se procese
        log.exception("Configuración avanzada del agente falló en la conversación %s", conv.id)
        await session.rollback()
        await session.refresh(conv)
    try:  # journeys: la respuesta del cliente (ramas «respondió», meta «replied», salida por opt-out) (§21.1)
        from app.journeys.hooks import on_inbound as journey_inbound

        await journey_inbound(session, conv, msg)
    except Exception:  # noqa: BLE001 — nunca debe impedir que el mensaje se procese
        log.exception("Journey: no se pudo registrar la respuesta en la conversación %s", conv.id)
        await session.rollback()
        await session.refresh(conv)
    # Orden: flujos (espera de respuesta, flujo del mensaje disparador, palabras clave) → automatizaciones → IA
    handled = await flows_inbound(session, conv, msg, link) or await on_inbound(session, conv, msg, is_new)
    await maybe_periodic(session, conv)
    await session.refresh(conv)
    if conv.status == "human":  # copiloto: sugerencias para el asesor (antirrebote, nunca lanza)
        __import__("app.copilot.hooks", fromlist=["on_inbound"]).on_inbound(conv.id, msg.id)
    return conv.status, conv.id, bool(handled)


# --- Messenger e Instagram --------------------------------------------------------
META_ATTACHMENT_TYPES = {"image": "image", "video": "video", "audio": "audio", "file": "document"}


async def process_meta_messaging(payload: dict) -> None:
    provider = "messenger" if payload.get("object") == "page" else "instagram"
    errors = []
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []) or []:
            try:
                await _handle_meta_event(provider, str(entry.get("id")), ev)
            except Exception as e:
                log.exception("Error procesando un evento de %s", provider)
                errors.append(f"{provider}: {type(e).__name__}: {e}")
    if errors:
        raise WebhookProcessingError("; ".join(errors)[:2000])


def _meta_referral(ev: dict) -> dict | None:
    """Referral de anuncio (Click to Messenger / Instagram Direct) en el evento, el mensaje o el postback."""
    ref = ev.get("referral") or (ev.get("message") or {}).get("referral") or (ev.get("postback") or {}).get("referral")
    return ref if isinstance(ref, dict) else None


async def _handle_meta_event(provider: str, entry_id: str, ev: dict) -> None:
    from app.channels.meta import MetaClient, channel_for_entry

    message = ev.get("message") or {}
    if message.get("is_echo") or ev.get("reaction") or message.get("is_deleted"):
        return  # eco de lo que envió la página, reacciones y borrados: no son mensajes del cliente
    sender = str((ev.get("sender") or {}).get("id") or "")
    async with SessionLocal() as session:
        channel = await channel_for_entry(session, provider, entry_id)
        if not channel:
            log.warning("Evento de %s para una cuenta no registrada (%s): se ignora", provider, entry_id)
            return
        if "delivery" in ev or "read" in ev:
            await _meta_status(session, channel, sender, ev)
            return
        if not sender or sender == entry_id or not (message or ev.get("postback") or ev.get("referral")):
            return
        referral = _meta_referral(ev)
        if not message and not ev.get("postback"):
            # Solo referral (el cliente abrió el chat desde un anuncio o enlace m.me): se guarda para su primer mensaje
            contact, ident, _new = await get_or_create_contact_by_identity(session, channel, sender)
            ident.profile = {**(ident.profile or {}), "pending_referral": referral}
            await session.commit()
            return
        mid = message.get("mid") or (ev.get("postback") or {}).get("mid") or f"{provider}.{sender}.{ev.get('timestamp')}"
        if await session.get(MessageWaId, mid):
            return  # Meta reintenta webhooks
        org = channel.organization_id
        await set_actor(session, "contact")
        client = MetaClient(channel, await _page_token(session, channel), sender)
        contact, ident, is_new = await get_or_create_contact_by_identity(session, channel, sender)
        if is_new:
            prof = await client.user_profile(sender)
            if prof:
                contact.name = contact.name or prof.get("name") or (f"@{prof['username']}" if prof.get("username") else None)
                contact.avatar_url = prof.get("profile_pic")
                ident.username, ident.profile = prof.get("username"), {k: v for k, v in prof.items() if k != "id"}
        if contact.blocked:
            await session.commit()
            return
        if not referral and (ident.profile or {}).get("pending_referral"):
            referral = ident.profile["pending_referral"]
            ident.profile = {k: v for k, v in ident.profile.items() if k != "pending_referral"}
        now = utcnow()
        ident.last_inbound_at = now
        contact.last_seen_at = now
        conv = await get_or_create_conversation(session, channel, contact)
        ad_id = (referral or {}).get("ad_id")
        if ad_id:
            ctx = (referral or {}).get("ads_context_data") or {}
            conv.ad_source_type, conv.ad_source_id = "ad", str(ad_id)
            conv.ad_headline = (ctx.get("ad_title") or "")[:500] or None
            conv.ad_source_url = ctx.get("photo_url") or ctx.get("video_url")

        text = message.get("text") or (ev.get("postback") or {}).get("title")
        attachments = message.get("attachments") or []
        att = attachments[0] if attachments else None
        mtype = "text"
        if att:
            mtype = META_ATTACHMENT_TYPES.get(att.get("type"), "unsupported")
            if att.get("type") == "story_mention":
                text = text or "[Te mencionó en una historia]"
            elif mtype == "unsupported":
                text = text or f"[{att.get('type')}]"
        msg = Message(direction="in", sender_type="contact", type=mtype if text or att else "unsupported", text=text,
                      wa_message_id=mid, status="received",
                      metadata_={"quick_reply": (message.get("quick_reply") or {}).get("payload")} if message.get(
                          "quick_reply") else None)
        url = ((att or {}).get("payload") or {}).get("url")
        if att and mtype != "unsupported" and url:
            try:
                data, mime = await client.download_media(url)
                mime = mime.split(";")[0]
                msg.media_path = await storage.upload(
                    storage.new_path(storage.MEDIA_BUCKET, org, f"conv/{conv.id}", mime), data, mime)
                msg.media_mime, msg.media_size = mime, len(data)
                if mtype == "audio":
                    msg.transcript = await transcribe(data, mime)
            except Exception as e:
                log.exception("No se pudo descargar el adjunto de %s", provider)
                msg.error = f"No se pudo descargar el archivo: {e}"[:2000]
        await record_message(session, conv, msg)
        ctx = (referral or {}).get("ads_context_data") or {}
        raw = {"referral": {"source_type": "ad", "source_id": str(ad_id), "source_url": conv.ad_source_url,
                            "headline": ctx.get("ad_title"), "image_url": ctx.get("photo_url"),
                            "video_url": ctx.get("video_url"),
                            "media_type": "video" if ctx.get("video_url") else ("image" if ctx.get("photo_url") else None)}
               } if ad_id else {}
        status, conv_id, handled = await after_inbound(session, conv, msg, raw, is_new, provider)
        client.last_inbound_at = now
    await client.mark_read()
    if status == "bot" and not handled:
        schedule_reply(conv_id)


async def _page_token(session, channel: Channel) -> str | None:
    from app.secrets_vault import get_secret

    return await get_secret(session, channel.access_token_secret_id)


async def _meta_status(session, channel: Channel, sender: str, ev: dict) -> None:
    """Entregado (por mid) y leído (marca de agua): actualiza los mensajes salientes de esa conversación."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    ident = await session.scalar(select(ContactIdentity).where(
        ContactIdentity.channel_id == channel.id, ContactIdentity.external_id == sender))
    if not ident:
        return
    conv_ids = list((await session.scalars(select(Conversation.id).where(
        Conversation.channel_id == channel.id, Conversation.contact_id == ident.contact_id))).all())
    if not conv_ids:
        return
    if "delivery" in ev:
        mids = (ev["delivery"] or {}).get("mids") or []
        if mids:
            await session.execute(update(Message).where(
                Message.conversation_id.in_(conv_ids), Message.wa_message_id.in_(mids),
                Message.status.in_(("pending", "sent"))).values(status="delivered"))
        watermark = (ev["delivery"] or {}).get("watermark")
        new_status, statuses = "delivered", ("pending", "sent")
    else:
        watermark = (ev["read"] or {}).get("watermark")
        new_status, statuses = "read", ("pending", "sent", "delivered")
    if watermark:
        until = datetime.fromtimestamp(int(watermark) / 1000, tz=UTC)
        await session.execute(update(Message).where(
            Message.conversation_id.in_(conv_ids), Message.direction == "out", Message.created_at <= until,
            Message.status.in_(statuses)).values(status=new_status))
    await session.commit()
