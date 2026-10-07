"""Etapas por línea de negocio con condición para la IA y tipificaciones enriquecidas (docs/data-model.md §17)."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_config import PIPELINE_LABELS, provision_default_stages, sync_pipeline_config
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, DealStageEvent, Group, Organization, PipelineStage, Typification

router = APIRouter(prefix="/api", tags=["pipeline-stages"])
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
SECTIONS = ("positive", "negative", "followup", "neutral")


class StageIn(BaseModel):
    key: str
    name: str = Field(min_length=1, max_length=120)
    external_name: str | None = None
    ai_condition: str | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    is_won: bool = False
    is_lost: bool = False
    is_active: bool = True


class StagesIn(BaseModel):
    stages: list[StageIn]


def stage_out(s: PipelineStage) -> dict:
    return {"id": s.id, "pipeline": s.pipeline, "key": s.key, "name": s.name, "external_name": s.external_name,
            "ai_condition": s.ai_condition, "position": s.position, "probability": s.probability,
            "is_won": s.is_won, "is_lost": s.is_lost, "is_active": s.is_active}


@router.get("/pipeline-stages")
async def all_stages(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Todas las líneas de negocio con sus etapas (orden de la línea)."""
    rows = (await session.scalars(select(PipelineStage).where(PipelineStage.organization_id == agent.organization_id)
                                  .order_by(PipelineStage.pipeline, PipelineStage.position, PipelineStage.id))).all()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.pipeline, {"pipeline": r.pipeline, "label": PIPELINE_LABELS.get(r.pipeline, r.pipeline),
                                    "stages": []})["stages"].append(stage_out(r))
    return list(out.values())


@router.get("/pipelines/{pipeline}/stages")
async def get_stages(pipeline: str, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(PipelineStage).where(
        PipelineStage.organization_id == agent.organization_id, PipelineStage.pipeline == pipeline)
        .order_by(PipelineStage.position, PipelineStage.id))).all()
    return [stage_out(r) for r in rows]


@router.put("/pipelines/{pipeline}/stages")
async def put_stages(pipeline: str, body: StagesIn, agent: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    """Reemplaza las etapas de la línea en el orden recibido (las que faltan se desactivan si ya tienen historial)."""
    org = agent.organization_id
    if not KEY_RE.match(pipeline):
        raise HTTPException(422, "La línea debe ser una clave en minúsculas (ej. nuevos, usados, taller)")
    keys = [s.key for s in body.stages]
    if not body.stages or len(keys) != len(set(keys)) or not all(KEY_RE.match(k) for k in keys):
        raise HTTPException(422, "Cada etapa necesita una clave única en minúsculas")
    if sum(s.is_won for s in body.stages) > 1 or sum(s.is_lost for s in body.stages) > 1:
        raise HTTPException(422, "Solo puede haber una etapa ganada y una perdida")
    if any(s.is_won and s.is_lost for s in body.stages):
        raise HTTPException(422, "Una etapa no puede ser ganada y perdida a la vez")
    existing = {r.key: r for r in (await session.scalars(select(PipelineStage).where(
        PipelineStage.organization_id == org, PipelineStage.pipeline == pipeline))).all()}
    for i, s in enumerate(body.stages):
        row = existing.pop(s.key, None) or PipelineStage(organization_id=org, pipeline=pipeline, key=s.key, name=s.name)
        row.name, row.external_name = s.name.strip(), (s.external_name or "").strip() or None
        row.ai_condition = (s.ai_condition or "").strip() or None
        row.probability, row.is_won, row.is_lost, row.is_active = s.probability, s.is_won, s.is_lost, s.is_active
        row.position = (i + 1) * 10
        session.add(row)
    for gone in existing.values():
        used = await session.scalar(select(func.count()).select_from(DealStageEvent).where(
            DealStageEvent.organization_id == org, DealStageEvent.to_stage == gone.key))
        if used:
            gone.is_active = False
        else:
            await session.delete(gone)
    await session.flush()
    await sync_pipeline_config(session, org)
    return await get_stages(pipeline, agent, session)


class DefaultsIn(BaseModel):
    industry: str | None = None


@router.post("/pipeline-stages/defaults")
async def defaults(body: DefaultsIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Crea las etapas sugeridas de la industria (solo en líneas sin etapas)."""
    org = await session.get(Organization, agent.organization_id)
    created = await provision_default_stages(session, org.id, body.industry or org.industry)
    await session.commit()
    return {"created": created, "pipelines": await all_stages(agent, session)}


@router.get("/deals/{deal_id}/stage-events")
async def stage_events(deal_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(DealStageEvent).where(
        DealStageEvent.deal_id == deal_id, DealStageEvent.organization_id == agent.organization_id)
        .order_by(DealStageEvent.created_at))).all()
    return [{"id": e.id, "from_stage": e.from_stage, "to_stage": e.to_stage, "source": e.source, "reason": e.reason,
             "confidence": e.confidence, "agent_id": e.agent_id, "conversation_id": e.conversation_id,
             "created_at": e.created_at} for e in rows]


# --- Tipificaciones ----------------------------------------------------------------------------------------
class TypificationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    criteria: str | None = None
    is_success: bool = False
    is_active: bool = True
    section: str | None = None
    keyword: str | None = None
    group_ids: list[int] = []
    reactivate_bot_after_h: float | None = Field(default=None, gt=0, le=720)
    required_fields: list[str] = []


def typification_out(t: Typification) -> dict:
    return {"id": t.id, "name": t.name, "criteria": t.criteria, "is_success": t.is_success, "is_active": t.is_active,
            "active": t.is_active, "position": t.position, "section": t.section, "keyword": t.keyword,
            "group_ids": list(t.group_ids or []),
            "reactivate_bot_after_h": float(t.reactivate_bot_after_h) if t.reactivate_bot_after_h is not None else None,
            "required_fields": list(t.required_fields or [])}


async def _validate_typification(session: AsyncSession, org: int, body: TypificationIn, typ_id: int | None) -> None:
    if body.section and body.section not in SECTIONS:
        raise HTTPException(422, "Sección inválida (positive, negative, followup o neutral)")
    dup = await session.scalar(select(Typification.id).where(Typification.organization_id == org,
                                                             Typification.name == body.name.strip()))
    if dup and dup != typ_id:
        raise HTTPException(409, "Ya existe una tipificación con ese nombre")
    if body.keyword:
        clash = await session.scalar(select(Typification.id).where(
            Typification.organization_id == org, func.lower(Typification.keyword) == body.keyword.strip().lower()))
        if clash and clash != typ_id:
            raise HTTPException(409, "Otra tipificación usa esa palabra clave")
    if body.group_ids:
        found = await session.scalar(select(func.count()).where(Group.organization_id == org,
                                                                Group.id.in_(body.group_ids)))
        if found != len(set(body.group_ids)):
            raise HTTPException(422, "Hay grupos que no existen")
    bad = [f for f in body.required_fields if not re.match(r"^(deal\.[a-z_]+|field:[a-z][a-z0-9_]*|[a-z][a-z0-9_]*)$", f)]
    if bad:
        raise HTTPException(422, f"Campos obligatorios inválidos: {', '.join(bad)}")


def _apply(t: Typification, body: TypificationIn) -> None:
    t.name, t.criteria = body.name.strip(), (body.criteria or "").strip() or None
    t.is_success, t.is_active, t.section = body.is_success, body.is_active, body.section
    t.keyword = (body.keyword or "").strip() or None
    t.group_ids, t.reactivate_bot_after_h = sorted(set(body.group_ids)), body.reactivate_bot_after_h
    t.required_fields = list(dict.fromkeys(body.required_fields))


@router.get("/typifications/detailed")
async def list_detailed(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Typification).where(Typification.organization_id == agent.organization_id)
                                  .order_by(Typification.position, Typification.name))).all()
    return [typification_out(t) for t in rows]


@router.post("/typifications")
async def create_typification(body: TypificationIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    await _validate_typification(session, agent.organization_id, body, None)
    position = (await session.scalar(select(func.max(Typification.position)).where(
        Typification.organization_id == agent.organization_id)) or 0) + 1
    t = Typification(organization_id=agent.organization_id, name=body.name.strip(), position=position)
    _apply(t, body)
    session.add(t)
    await session.commit()
    return typification_out(t)


@router.put("/typifications/{typ_id}")
async def update_typification(typ_id: int, body: TypificationIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    t = await session.get(Typification, typ_id)
    if not t or t.organization_id != agent.organization_id:
        raise HTTPException(404, "Tipificación no encontrada")
    await _validate_typification(session, agent.organization_id, body, t.id)
    _apply(t, body)
    await session.commit()
    return typification_out(t)


@router.delete("/typifications/{typ_id}")
async def delete_typification(typ_id: int, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    """Se desactiva (las conversaciones y reportes la conservan)."""
    t = await session.get(Typification, typ_id)
    if not t or t.organization_id != agent.organization_id:
        raise HTTPException(404, "Tipificación no encontrada")
    t.is_active = False
    await session.commit()
    return {"ok": True}
