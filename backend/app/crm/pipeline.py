"""Embudos de negocios (org_settings, clave 'deals')."""

import copy

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrgSetting, utcnow

DEFAULT = {
    "pipelines": {
        "default": {
            "label": "Ventas",
            "stages": [
                {"key": "new", "label": "Nuevo"},
                {"key": "qualified", "label": "Calificado"},
                {"key": "proposal", "label": "Propuesta"},
                {"key": "negotiation", "label": "Negociación"},
                {"key": "won", "label": "Ganado"},
                {"key": "lost", "label": "Perdido"},
            ],
        }
    },
    "currency": "COP",
}
CLOSED_STAGES = {"won": "won", "lost": "lost"}


async def get_pipelines(session: AsyncSession, org: int) -> dict:
    row = await session.get(OrgSetting, (org, "deals"))
    if row and isinstance(row.value, dict) and row.value.get("pipelines"):
        return {**copy.deepcopy(DEFAULT), **row.value}
    return copy.deepcopy(DEFAULT)


def validate(config: dict) -> list[str]:
    errors = []
    pipelines = config.get("pipelines")
    if not isinstance(pipelines, dict) or not pipelines:
        return ["Define al menos un embudo"]
    for key, p in pipelines.items():
        stages = (p or {}).get("stages") or []
        keys = [s.get("key") for s in stages]
        if not stages or not all(keys) or len(keys) != len(set(keys)):
            errors.append(f"El embudo «{key}» necesita etapas con clave única")
        if not {"won", "lost"} <= set(keys):
            errors.append(f"El embudo «{key}» debe tener las etapas «won» y «lost»")
    return errors


async def save_pipelines(session: AsyncSession, org: int, config: dict, agent_id: int | None) -> dict:
    row = await session.get(OrgSetting, (org, "deals"))
    value = {"pipelines": config["pipelines"], "currency": config.get("currency") or DEFAULT["currency"]}
    if row:
        row.value, row.updated_by, row.updated_at = value, agent_id, utcnow()
    else:
        session.add(OrgSetting(organization_id=org, key="deals", value=value, updated_by=agent_id))
    await session.commit()
    return await get_pipelines(session, org)


def stage_keys(config: dict, pipeline: str) -> list[str]:
    p = config["pipelines"].get(pipeline) or {}
    return [s["key"] for s in p.get("stages") or []]
