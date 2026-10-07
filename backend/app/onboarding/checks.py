"""Validaciones del número de WhatsApp (channel_health_checks): se corren en el asistente, con «Arreglar» para
las que el sistema puede corregir, y luego periódicamente para el Centro de Control."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Channel, ChannelHealthCheck, OnboardingRun, WaTemplate, utcnow
from app.onboarding import graph, meta, packs

log = logging.getLogger(__name__)

CHECKS: dict[str, str] = {
    "token_valid": "Token de acceso válido",
    "registered": "Número registrado en la API de WhatsApp",
    "pin": "Verificación en dos pasos (PIN)",
    "webhook": "App suscrita a los webhooks de la cuenta",
    "name_approved": "Nombre visible aprobado por Meta",
    "quality": "Calidad del número",
    "limit_tier": "Límite de conversaciones iniciadas por la empresa",
    "profile": "Perfil de empresa completo",
    "templates_ready": "Plantillas aprobadas",
    "outbound_test": "Mensaje de prueba enviado",
    "inbound_roundtrip": "Respuesta de prueba recibida",
}
REQUIRED_FOR_GO_LIVE = ("token_valid", "registered", "webhook", "templates_ready")


def out(row: ChannelHealthCheck, extra: dict | None = None) -> dict:
    return {"check_key": row.check_key, "label": CHECKS.get(row.check_key, row.check_key), "status": row.status,
            "detail": row.detail, "fixable": row.fixable, "checked_at": row.checked_at, "data": row.data,
            **(extra or {})}


async def _save(session: AsyncSession, channel: Channel, key: str, status: str, detail: str | None,
                fixable: bool = False, data: dict | None = None) -> ChannelHealthCheck:
    row = await session.get(ChannelHealthCheck, (channel.id, key))
    if row is None:
        row = ChannelHealthCheck(channel_id=channel.id, check_key=key, organization_id=channel.organization_id)
        session.add(row)
    row.status, row.detail, row.fixable, row.data, row.checked_at = status, detail, fixable, data or {}, utcnow()
    return row


async def list_checks(session: AsyncSession, channel_id: int | None) -> list[dict]:
    if not channel_id:
        return []
    rows = {r.check_key: r for r in (await session.scalars(
        select(ChannelHealthCheck).where(ChannelHealthCheck.channel_id == channel_id))).all()}
    return [out(rows[k]) for k in CHECKS if k in rows]


async def _templates_check(session: AsyncSession, channel: Channel) -> tuple[str, str, bool, dict]:
    rows = (await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == channel.organization_id, WaTemplate.waba_id == channel.waba_id))).all()
    approved = [r.name for r in rows if r.status == "APPROVED"]
    pending = [r.name for r in rows if r.status in ("PENDING", "IN_APPEAL")]
    rejected = [r.name for r in rows if r.status == "REJECTED" and r.source == "onboarding"]
    data = {"approved": approved, "pending": pending, "rejected": rejected}
    own_approved = [r.name for r in rows if r.status == "APPROVED" and r.name != "hello_world"]
    if own_approved:
        return "pass", f"{len(own_approved)} aprobadas", False, data
    if pending:
        return "warn", f"{len(pending)} en revisión de Meta (suele tardar minutos u horas)", False, data
    if rejected:
        return "fail", f"{len(rejected)} rechazadas: reescríbelas con IA", True, data
    return "fail", "Aún no hay plantillas propias: envía el paquete recomendado", True, data


async def run_checks(session: AsyncSession, channel: Channel, run: OnboardingRun | None = None) -> list[dict]:
    """Corre todos los chequeos y guarda el último resultado de cada uno."""
    if channel.provider != "whatsapp_cloud":
        return []
    token = await meta.channel_token(session, channel)
    results: list[ChannelHealthCheck] = []
    if not token:
        results.append(await _save(session, channel, "token_valid", "fail",
                                   "No hay token: vuelve a conectar el número con Meta"))
        await session.flush()
        return [out(r) for r in results]
    try:
        await meta.sync_phone_state(session, channel, token)
        results.append(await _save(session, channel, "token_valid", "pass", "Meta respondió con el token del número"))
    except graph.GraphError as e:
        expired = e.code == 190
        results.append(await _save(session, channel, "token_valid", "fail",
                                   "El token venció o fue revocado: vuelve a conectar el número" if expired
                                   else f"Meta: {e}"))
        await session.flush()
        return [out(r) for r in results]

    if channel.is_coexistence or channel.platform_type == "CLOUD_API" or channel.registered_at:
        results.append(await _save(session, channel, "registered", "pass",
                                   "Coexistencia con la app WhatsApp Business" if channel.is_coexistence
                                   else "Registrado en Cloud API"))
    else:
        results.append(await _save(session, channel, "registered", "fail",
                                   "El número no está registrado en Cloud API", fixable=True))

    if channel.is_coexistence or channel.pin_set_at:
        results.append(await _save(session, channel, "pin", "pass", "PIN configurado"))
    else:
        results.append(await _save(session, channel, "pin", "warn",
                                   "Sin PIN de verificación en dos pasos registrado por la plataforma", fixable=True))

    try:
        subs = await graph.call("GET", f"{channel.waba_id}/subscribed_apps", token)
        app_id = get_settings().meta_app_id
        apps = [(a.get("whatsapp_business_api_data") or {}).get("id") for a in subs.get("data") or []]
        ok = bool(apps) and (not app_id or app_id in apps)
        if ok:
            channel.webhook_subscribed_at = channel.webhook_subscribed_at or utcnow()
        results.append(await _save(session, channel, "webhook", "pass" if ok else "fail",
                                   "Webhooks activos" if ok else "La app no está suscrita a la cuenta de WhatsApp",
                                   fixable=not ok))
    except graph.GraphError as e:
        results.append(await _save(session, channel, "webhook", "fail", f"Meta: {e}", fixable=True))

    name = channel.name_status
    if name in ("APPROVED", "AVAILABLE_WITHOUT_REVIEW"):
        results.append(await _save(session, channel, "name_approved", "pass", f"«{channel.verified_name}» aprobado"))
    elif name in ("DECLINED", "NON_EXISTS"):
        results.append(await _save(session, channel, "name_approved", "fail",
                                   "Meta rechazó el nombre visible: cámbialo en el Administrador de WhatsApp"))
    else:
        results.append(await _save(session, channel, "name_approved", "warn",
                                   f"Nombre en revisión ({name or 'sin estado'}): puedes operar mientras tanto"))

    quality = channel.quality_rating
    q_status = {"GREEN": "pass", "YELLOW": "warn", "RED": "fail"}.get(quality or "", "pass")
    results.append(await _save(session, channel, "quality", q_status,
                               {"GREEN": "Alta", "YELLOW": "Media: revisa quejas y bloqueos", "RED": "Baja: Meta puede "
                                "limitar el número"}.get(quality or "", "Sin datos todavía (número nuevo)")))

    tier = channel.messaging_limit_tier
    results.append(await _save(session, channel, "limit_tier", "warn" if tier in ("TIER_50", "TIER_250") else "pass",
                               f"{tier or 'sin dato'}" + (": sube al verificar la empresa y mantener buena calidad"
                                                          if tier in ("TIER_50", "TIER_250") else "")))

    prof = channel.business_profile or {}
    if prof.get("about") or prof.get("description"):
        results.append(await _save(session, channel, "profile", "pass", "Perfil publicado"))
    else:
        results.append(await _save(session, channel, "profile", "warn", "Falta descripción y datos de contacto",
                                   fixable=True))

    try:
        await meta.refresh_template_statuses(session, channel, token)
    except graph.GraphError as e:
        log.info("No se pudo refrescar plantillas: %s", e)
    status, detail, fixable, data = await _templates_check(session, channel)
    results.append(await _save(session, channel, "templates_ready", status, detail, fixable, data))

    test = ((run.answers if run else {}) or {}).get("test") or {}
    if test.get("message_id"):
        results.append(await _save(session, channel, "outbound_test", "pass", f"Enviado a +{test.get('phone')}"))
        got = bool(test.get("replied"))  # lo marca hooks.on_whatsapp_inbound al llegar la respuesta
        results.append(await _save(session, channel, "inbound_roundtrip", "pass" if got else "pending",
                                   "Respuesta recibida: los webhooks llegan" if got
                                   else "Responde el mensaje de prueba desde tu teléfono"))
    else:
        results.append(await _save(session, channel, "outbound_test", "pending", "Envía un mensaje de prueba"))
        results.append(await _save(session, channel, "inbound_roundtrip", "pending", "Pendiente del mensaje de prueba"))
    await session.flush()
    return [out(r) for r in results]


async def fix(session: AsyncSession, channel: Channel, key: str, run: OnboardingRun | None,
              agent_id: int | None) -> dict:
    """Corrige un chequeo. Devuelve el chequeo actualizado (con `pin` si se generó uno, solo en la respuesta)."""
    token = await meta.channel_token(session, channel)
    if not token:
        raise graph.GraphError("No hay token: vuelve a conectar el número con Meta")
    extra: dict = {}
    if key == "webhook":
        await graph.call("POST", f"{channel.waba_id}/subscribed_apps", token)
        channel.webhook_subscribed_at = utcnow()
    elif key == "registered":
        pin = meta.new_pin()
        await graph.call("POST", f"{channel.phone_number_id}/register", token,
                         json={"messaging_product": "whatsapp", "pin": pin})
        channel.registered_at = channel.pin_set_at = utcnow()
        extra["pin"] = pin
    elif key == "pin":
        pin = meta.new_pin()
        await graph.call("POST", channel.phone_number_id, token, json={"pin": pin})
        channel.pin_set_at = utcnow()
        extra["pin"] = pin
    elif key == "profile":
        await meta.push_profile(session, channel, token,
                                meta.profile_from_answers((run.answers if run else {}) or {}, run.industry if run else None))
    elif key == "templates_ready":
        from app.onboarding import service  # import diferido (service usa este módulo)

        await service.submit_pack(session, channel, run, None, {}, agent_id)
    else:
        raise graph.GraphError("Este chequeo no se corrige automáticamente")
    await session.flush()
    results = {c["check_key"]: c for c in await run_checks(session, channel, run)}
    row = results.get(key) or {"check_key": key, "label": CHECKS.get(key, key)}
    return {**row, **extra}


def packs_for(run: OnboardingRun | None) -> dict:
    return packs.pack(run.industry if run else None, (run.answers if run else {}) or {})
