"""Plantillas de WhatsApp (HSM): copia sincronizada en wa_templates y armado de componentes."""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Channel, WaTemplate, utcnow

settings = get_settings()
VAR = re.compile(r"\{\{\s*([\w]+)\s*\}\}")
SYNC_TTL = timedelta(minutes=5)


def describe(t: dict) -> dict:
    """Resume una plantilla: texto del cuerpo, variables y si la podemos enviar."""
    body = next((c for c in t.get("components", []) if c.get("type") == "BODY"), {})
    header = next((c for c in t.get("components", []) if c.get("type") == "HEADER"), None)
    body_text = body.get("text", "")
    variables = list(dict.fromkeys(VAR.findall(body_text)))
    unsupported = None
    if header and header.get("format") in ("IMAGE", "VIDEO", "DOCUMENT", "LOCATION"):
        unsupported = "Encabezado multimedia (aún no soportado)"
    elif header and VAR.search(header.get("text", "")):
        unsupported = "Encabezado con variables (aún no soportado)"
    return {
        "name": t["name"],
        "language": t["language"],
        "status": t.get("status"),
        "category": t.get("category"),
        "header": header.get("text") if header and header.get("format") == "TEXT" else None,
        "body": body_text,
        "variables": variables,
        "named": t.get("parameter_format") == "NAMED" or any(not v.isdigit() for v in variables),
        "supported": unsupported is None,
        "unsupported_reason": unsupported,
    }


def _row_to_raw(r: WaTemplate) -> dict:
    return {"name": r.name, "language": r.language, "status": r.status, "category": r.category,
            "components": r.components, "parameter_format": r.parameter_format}


async def list_templates(session: AsyncSession, channel: Channel, refresh: bool = False) -> list[dict]:
    """Plantillas de la WABA del canal. Se sincronizan desde Meta cada 5 min (o al pedir refresh)."""
    # La WABA del servidor solo aplica a la empresa de la instalación (ver routers/config.server_waba)
    waba = channel.waba_id or (settings.wa_waba_id if channel.organization_id == settings.organization_id else None)
    if not waba:
        return []
    org = channel.organization_id
    rows = list((await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == org, WaTemplate.waba_id == waba).order_by(WaTemplate.name))).all())
    fresh = rows and all(r.synced_at and utcnow() - r.synced_at < SYNC_TTL for r in rows)
    if refresh or not fresh:
        from app.service import wa_client  # import diferido (service importa este módulo indirectamente)

        raw = await (await wa_client(session, channel)).list_templates(waba)
        # Upsert: se conservan los datos propios (origen, paquete del asistente, motivo de rechazo)
        existing = {(r.name, r.language): r for r in rows}
        now = utcnow()
        rows = []
        for t in raw:
            r = existing.pop((t["name"], t["language"]), None)
            if r is None:
                r = WaTemplate(organization_id=org, waba_id=waba, name=t["name"], language=t["language"],
                               components=t.get("components", []))
                session.add(r)
            r.category, r.status = t.get("category"), t.get("status")
            r.quality = (t.get("quality_score") or {}).get("score") if isinstance(t.get("quality_score"), dict) else None
            r.parameter_format, r.components, r.synced_at = t.get("parameter_format"), t.get("components", []), now
            if t.get("id"):
                r.meta_template_id = str(t["id"])
            if t.get("rejected_reason") and t.get("rejected_reason") != "NONE":
                r.rejected_reason = t["rejected_reason"]
            rows.append(r)
        for gone in existing.values():  # borradas en Meta
            await session.delete(gone)
        await session.commit()
        rows.sort(key=lambda r: r.name)
    return [describe(_row_to_raw(r)) for r in rows]


async def invalidate(session: AsyncSession, org: int) -> None:
    """Fuerza la próxima sincronización (p. ej. al recibir message_template_status_update)."""
    for r in (await session.scalars(select(WaTemplate).where(WaTemplate.organization_id == org))).all():
        r.synced_at = utcnow() - SYNC_TTL * 2


def build(tpl: dict, values: list[str]) -> tuple[list[dict], str]:
    """Devuelve (components, texto renderizado) para enviar y guardar en el historial."""
    if len(values) != len(tpl["variables"]):
        raise ValueError(f"La plantilla requiere {len(tpl['variables'])} variables")
    rendered = tpl["body"]
    params = []
    for name, value in zip(tpl["variables"], values, strict=True):
        rendered = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", value, rendered)
        p = {"type": "text", "text": value}
        if tpl["named"]:
            p["parameter_name"] = name
        params.append(p)
    components = [{"type": "body", "parameters": params}] if params else []
    if tpl.get("header"):
        rendered = f"*{tpl['header']}*\n{rendered}"
    return components, rendered


def personalize(values: list[str], contact_name: str | None) -> list[str]:
    """Reemplaza {{nombre}} en los valores de una campaña por el nombre del contacto."""
    name = (contact_name or "").split(" ")[0] or "cliente"
    return [v.replace("{{nombre}}", name) for v in values]
