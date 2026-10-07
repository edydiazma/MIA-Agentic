"""Automatización con Meta: conectar el número (Embedded Signup, incluida coexistencia con la app WhatsApp
Business), leer su estado, perfil de empresa, plantillas y mensaje de prueba. Todo pasa por graph.call."""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AIAgent, Channel, WaTemplate, utcnow
from app.onboarding import graph, packs
from app.secrets_vault import get_secret, put_secret

log = logging.getLogger(__name__)

PHONE_FIELDS = ("display_phone_number,verified_name,code_verification_status,quality_rating,platform_type,"
                "throughput,name_status,new_name_status,messaging_limit_tier,is_official_business_account")
PROFILE_FIELDS = "about,address,description,email,profile_picture_url,websites,vertical"
VERTICALS = {"automotriz": "AUTO", "salud": "HEALTH", "educacion": "EDU", "retail": "RETAIL",
             "servicios": "PROF_SERVICES", "inmobiliaria": "OTHER", "otro": "OTHER"}


async def channel_token(session: AsyncSession, channel: Channel) -> str | None:
    if channel.access_token_secret_id:
        token = await get_secret(session, channel.access_token_secret_id)
        if token:
            return token
    return get_settings().wa_access_token or None


def new_pin() -> str:
    return f"{secrets.randbelow(10**6):06d}"


async def exchange_code(code: str) -> str:
    env = get_settings()
    data = await graph.call("GET", "oauth/access_token", params={
        "client_id": env.meta_app_id, "client_secret": env.meta_app_secret, "code": code})
    token = data.get("access_token")
    if not token:
        raise graph.GraphError("Meta no devolvió un token")
    return token


async def connect_whatsapp(session: AsyncSession, org: int, *, code: str, waba_id: str, phone_number_id: str,
                           pin: str | None = None, generate_pin: bool = False, coexistence: bool = False,
                           name: str | None = None) -> tuple[Channel, str | None, dict]:
    """Embedded Signup completo. Devuelve (canal, PIN generado o None, detalle de lo hecho).

    - Coexistencia: el número sigue en la app WhatsApp Business: no se registra; se pide a Meta sincronizar
      contactos e historial (POST /{phone}/smb_app_data, dentro de las 24 h siguientes).
    - Si no: registro en Cloud API con el PIN de verificación en dos pasos (generado si no viene)."""
    token = await exchange_code(code)
    detail: dict = {}
    await graph.call("POST", f"{waba_id}/subscribed_apps", token)
    detail["subscribed"] = True
    generated = None
    if coexistence:
        for sync_type in ("smb_app_state_sync", "history"):
            try:
                await graph.call("POST", f"{phone_number_id}/smb_app_data", token,
                                 json={"messaging_product": "whatsapp", "sync_type": sync_type})
                detail[sync_type] = "requested"
            except graph.GraphError as e:  # no bloquea la conexión: queda en el detalle
                detail[sync_type] = f"error: {e}"
    elif pin or generate_pin:
        if not pin:
            pin = generated = new_pin()  # se muestra una sola vez al administrador
        await graph.call("POST", f"{phone_number_id}/register", token,
                         json={"messaging_product": "whatsapp", "pin": pin})
        detail["registered"] = True
    info = await graph.call("GET", phone_number_id, token, params={"fields": "display_phone_number,verified_name"})

    channel = await session.scalar(select(Channel).where(Channel.phone_number_id == phone_number_id))
    if channel is None:
        channel = Channel(organization_id=org, phone_number_id=phone_number_id, provider="whatsapp_cloud",
                          name=name or info.get("verified_name") or "WhatsApp")
        channel.default_ai_agent_id = await session.scalar(select(AIAgent.id).where(
            AIAgent.organization_id == org).order_by(AIAgent.id).limit(1))
        session.add(channel)
        await session.flush()
    now = utcnow()
    channel.waba_id = waba_id
    channel.display_phone = info.get("display_phone_number")
    channel.verified_name = info.get("verified_name")
    channel.is_coexistence = coexistence
    channel.webhook_subscribed_at = now
    if detail.get("registered"):
        channel.registered_at = channel.pin_set_at = now
    channel.status, channel.last_error = "active", None
    channel.access_token_secret_id = await put_secret(session, token, f"channel:{channel.id}",
                                                      channel.access_token_secret_id)
    await session.flush()
    return channel, generated, detail


async def sync_phone_state(session: AsyncSession, channel: Channel, token: str) -> dict:
    """Lee el estado del número en Meta y lo guarda en el canal."""
    info = await graph.call("GET", channel.phone_number_id, token, params={"fields": PHONE_FIELDS})
    channel.display_phone = info.get("display_phone_number") or channel.display_phone
    channel.verified_name = info.get("verified_name") or channel.verified_name
    channel.code_verification_status = info.get("code_verification_status")
    channel.quality_rating = info.get("quality_rating")
    channel.platform_type = info.get("platform_type")
    channel.name_status = info.get("new_name_status") or info.get("name_status")
    channel.messaging_limit_tier = info.get("messaging_limit_tier")
    throughput = info.get("throughput")
    channel.throughput_level = throughput.get("level") if isinstance(throughput, dict) else throughput
    if channel.platform_type == "CLOUD_API" and not channel.registered_at:
        channel.registered_at = utcnow()
    channel.meta_synced_at = utcnow()
    return info


def profile_from_answers(answers: dict, industry: str | None) -> dict:
    """Perfil de empresa de WhatsApp desde las respuestas (respeta los límites de Meta)."""
    c = answers.get("company") or {}
    imported = ((answers.get("import") or answers.get("website_import") or {}).get("profile")) or {}
    about = (c.get("about") or imported.get("about") or c.get("description") or "").strip()
    description = (c.get("description") or imported.get("description") or "").strip()
    website = (c.get("website") or imported.get("website") or "").strip()
    out = {"messaging_product": "whatsapp", "vertical": VERTICALS.get(packs.industry_or_default(industry), "OTHER")}
    if about:
        out["about"] = about[:139]
    if description:
        out["description"] = description[:512]
    if c.get("address") or imported.get("address"):
        out["address"] = (c.get("address") or imported.get("address"))[:256]
    if c.get("email") or imported.get("email"):
        out["email"] = (c.get("email") or imported.get("email"))[:128]
    if website:
        out["websites"] = [website if website.startswith("http") else f"https://{website}"][:2]
    return out


async def push_profile(session: AsyncSession, channel: Channel, token: str, profile: dict) -> dict:
    await graph.call("POST", f"{channel.phone_number_id}/whatsapp_business_profile", token, json=profile)
    channel.business_profile = {k: v for k, v in profile.items() if k != "messaging_product"}
    return channel.business_profile


async def read_profile(channel: Channel, token: str) -> dict:
    data = await graph.call("GET", f"{channel.phone_number_id}/whatsapp_business_profile", token,
                            params={"fields": PROFILE_FIELDS})
    rows = data.get("data") or []
    return rows[0] if rows else {}


async def submit_template(session: AsyncSession, channel: Channel, token: str, t: dict, *, pack_key: str | None,
                          agent_id: int | None) -> WaTemplate:
    """Crea la plantilla en la WABA y la guarda en wa_templates (source onboarding). Si ya existe con ese
    nombre e idioma, adopta la de Meta en lugar de fallar."""
    error = packs.validate(t)
    if error:
        raise graph.GraphError(error)
    components = packs.to_components(t)
    payload = {"name": t["name"], "language": t.get("language") or packs.LANGUAGE, "category": t["category"],
               "components": components}
    status, meta_id = "PENDING", None
    try:
        data = await graph.call("POST", f"{channel.waba_id}/message_templates", token, json=payload)
        status, meta_id = data.get("status") or "PENDING", data.get("id")
    except graph.GraphError as e:
        if "exist" not in str(e).lower():
            raise
        found = await graph.call("GET", f"{channel.waba_id}/message_templates", token,
                                 params={"name": t["name"], "fields": "id,name,language,status,category"})
        match = next((x for x in found.get("data") or [] if x.get("language") == payload["language"]), None)
        if not match:
            raise
        status, meta_id = match.get("status") or "PENDING", match.get("id")
    row = await session.scalar(select(WaTemplate).where(
        WaTemplate.organization_id == channel.organization_id, WaTemplate.waba_id == channel.waba_id,
        WaTemplate.name == t["name"], WaTemplate.language == payload["language"]))
    if row is None:
        row = WaTemplate(organization_id=channel.organization_id, waba_id=channel.waba_id, name=t["name"],
                         language=payload["language"], components=components)
        session.add(row)
    row.category, row.status, row.components = t["category"], status, components
    row.source, row.pack_key, row.template_key = "onboarding", pack_key, t.get("template_key")
    row.meta_template_id, row.rejected_reason = meta_id, None
    row.submitted_by, row.submitted_at, row.synced_at = agent_id, utcnow(), utcnow()
    await session.flush()
    return row


async def refresh_template_statuses(session: AsyncSession, channel: Channel, token: str) -> list[WaTemplate]:
    """Actualiza estado y motivo de rechazo de las plantillas del asistente (respaldo del webhook)."""
    rows = list((await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == channel.organization_id, WaTemplate.waba_id == channel.waba_id,
        WaTemplate.source == "onboarding"))).all())
    if not rows:
        return rows
    data = await graph.call("GET", f"{channel.waba_id}/message_templates", token,
                            params={"fields": "id,name,language,status,category,rejected_reason", "limit": 200})
    by_key = {(x.get("name"), x.get("language")): x for x in data.get("data") or []}
    for r in rows:
        x = by_key.get((r.name, r.language))
        if x:
            r.status = x.get("status") or r.status
            r.meta_template_id = x.get("id") or r.meta_template_id
            reason = x.get("rejected_reason")
            r.rejected_reason = reason if reason and reason != "NONE" else (r.rejected_reason if r.status == "REJECTED"
                                                                             else None)
            r.synced_at = utcnow()
    return rows


async def send_test(channel: Channel, token: str, phone: str, template: WaTemplate | None) -> str | None:
    """Mensaje de prueba: hello_world (siempre aprobada) o una plantilla aprobada del asistente."""
    if template is not None:
        parts = packs.from_components(template.components)
        params = [{"type": "text", "text": v} for v in parts["example_values"]]
        comps = [{"type": "body", "parameters": params}] if params else []
        body = {"name": template.name, "language": {"code": template.language}, "components": comps}
    else:
        body = {"name": "hello_world", "language": {"code": "en_US"}}
    data = await graph.call("POST", f"{channel.phone_number_id}/messages", token, json={
        "messaging_product": "whatsapp", "to": phone, "type": "template", "template": body})
    msgs = data.get("messages") or []
    return msgs[0].get("id") if msgs else None


async def on_template_status(session: AsyncSession, org: int, v: dict) -> None:
    """Webhook message_template_status_update: estado y motivo de rechazo al instante."""
    q = select(WaTemplate).where(WaTemplate.organization_id == org)
    if v.get("message_template_id"):
        q = q.where((WaTemplate.meta_template_id == str(v["message_template_id"]))
                    | (WaTemplate.name == v.get("message_template_name")))
    else:
        q = q.where(WaTemplate.name == v.get("message_template_name"))
    if v.get("message_template_language"):
        q = q.where(WaTemplate.language == v["message_template_language"])
    for row in (await session.scalars(q)).all():
        event = v.get("event")
        if event:
            row.status = event
        if event == "REJECTED":
            row.rejected_reason = v.get("reason") or (v.get("other_info") or {}).get("description") or "REJECTED"
        elif event == "APPROVED":
            row.rejected_reason = None
        row.synced_at = utcnow()
