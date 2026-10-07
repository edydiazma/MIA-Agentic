"""Negocios (deals): CRUD, tablero por etapa, ganar/perder y embudos configurables."""

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.crm import pipeline as pl
from app.db import get_session, set_actor
from app.models import Agent, Contact, Conversation, Deal, ExternalLink, IntegrationConnection, utcnow
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/deals", tags=["deals"])


class DealIn(BaseModel):
    contact_id: int
    name: str
    conversation_id: int | None = None
    owner_agent_id: int | None = None
    amount: float | None = None
    currency: str | None = None
    pipeline: str = "default"
    stage: str | None = None
    expected_close: date | None = None


class DealUpdate(BaseModel):
    name: str | None = None
    conversation_id: int | None = None
    owner_agent_id: int | None = None
    amount: float | None = None
    currency: str | None = None
    pipeline: str | None = None
    stage: str | None = None
    expected_close: date | None = None


class WinIn(BaseModel):
    amount: float | None = None


class LoseIn(BaseModel):
    reason: str | None = None


class PipelinesIn(BaseModel):
    pipelines: dict
    currency: str | None = None


class DealOut(BaseModel):
    id: int
    name: str
    contact: dict
    conversation_id: int | None
    owner: dict | None
    amount: float | None
    currency: str
    pipeline: str
    stage: str
    status: str
    source: str
    expected_close: date | None
    closed_at: UTCDateTime | None
    lost_reason: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    crm_links: list[dict] = []


async def deal_out(session: AsyncSession, d: Deal) -> DealOut:
    links = (await session.execute(
        select(IntegrationConnection.provider, ExternalLink.remote_id).join(
            IntegrationConnection, IntegrationConnection.id == ExternalLink.connection_id)
        .where(ExternalLink.local_type == "deal", ExternalLink.local_id == d.id))).all()
    return DealOut(
        id=d.id, name=d.name, contact={"id": d.contact.id, "name": d.contact.name, "wa_id": d.contact.wa_id},
        conversation_id=d.conversation_id,
        owner={"id": d.owner.id, "name": d.owner.name} if d.owner else None,
        amount=float(d.amount) if d.amount is not None else None, currency=d.currency, pipeline=d.pipeline,
        stage=d.stage, status=d.status, source=d.source, expected_close=d.expected_close, closed_at=d.closed_at,
        lost_reason=d.lost_reason, created_at=d.created_at, updated_at=d.updated_at,
        crm_links=[{"provider": p, "remote_id": r} for p, r in links])


async def _deal(session: AsyncSession, deal_id: int, org: int) -> Deal:
    d = await session.get(Deal, deal_id)
    if not d or d.organization_id != org:
        raise HTTPException(404, "Negocio no encontrado")
    return d


async def _validate_refs(session: AsyncSession, org: int, data: dict) -> None:
    if data.get("contact_id") is not None:
        c = await session.get(Contact, data["contact_id"])
        if not c or c.organization_id != org:
            raise HTTPException(422, "Cliente inválido")
    if data.get("conversation_id") is not None:
        conv = await session.get(Conversation, data["conversation_id"])
        if not conv or conv.organization_id != org:
            raise HTTPException(422, "Conversación inválida")
    if data.get("owner_agent_id") is not None:
        a = await session.get(Agent, data["owner_agent_id"])
        if not a or a.organization_id != org:
            raise HTTPException(422, "Asesor inválido")
    if data.get("amount") is not None and data["amount"] < 0:
        raise HTTPException(422, "El monto no puede ser negativo")
    if "name" in data and data["name"] is not None and not data["name"].strip():
        raise HTTPException(422, "El nombre es obligatorio")


async def _log_event(session: AsyncSession, deal: Deal, event: str, agent_id: int | None) -> None:
    """Hecho deal_won / deal_lost en la conversación vinculada (reportes y conversiones)."""
    if not deal.conversation_id:
        return
    await set_actor(session, "agent" if agent_id else "system", agent_id)
    await session.execute(text(
        "select public.log_conversation_event(c, :ev, cast(:payload as jsonb)) from public.conversations c "
        "where c.id = :cid"), {"ev": event, "cid": deal.conversation_id, "payload": json.dumps(
            {"deal_id": deal.id, "amount": None if deal.amount is None else float(deal.amount),
             "currency": deal.currency})})


def _apply_stage(deal: Deal, stage: str, stages: list[str]) -> str | None:
    """Mueve de etapa; won/lost cierran el negocio. Devuelve el evento a registrar (si lo hay)."""
    if stage not in stages:
        raise HTTPException(422, f"Etapa inválida: {stage}")
    previous = deal.status
    deal.stage = stage
    if stage in pl.CLOSED_STAGES:
        deal.status = pl.CLOSED_STAGES[stage]
        deal.closed_at = deal.closed_at if previous == deal.status else utcnow()
    else:
        deal.status, deal.closed_at, deal.lost_reason = "open", None, None
    if deal.status != previous and deal.status in ("won", "lost"):
        return f"deal_{deal.status}"
    return None


# --- Embudos --------------------------------------------------------------------
@router.get("/pipelines")
async def get_pipelines(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await pl.get_pipelines(session, agent.organization_id)


@router.put("/pipelines")
async def put_pipelines(body: PipelinesIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    errors = pl.validate(body.model_dump())
    if errors:
        raise HTTPException(422, "; ".join(errors))
    return await pl.save_pipelines(session, agent.organization_id, body.model_dump(), agent.id)


# --- Listado y tablero ----------------------------------------------------------
def _filtered(org: int, status: str | None, stage: str | None, owner_id: int | None, contact_id: int | None,
              q: str | None, pipeline: str | None):
    stmt = select(Deal).join(Contact, Contact.id == Deal.contact_id).where(Deal.organization_id == org)
    if status:
        stmt = stmt.where(Deal.status == status)
    if stage:
        stmt = stmt.where(Deal.stage == stage)
    if owner_id:
        stmt = stmt.where(Deal.owner_agent_id == owner_id)
    if contact_id:
        stmt = stmt.where(Deal.contact_id == contact_id)
    if pipeline:
        stmt = stmt.where(Deal.pipeline == pipeline)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Deal.name.ilike(like), Contact.name.ilike(like), Contact.wa_id.ilike(like)))
    return stmt


@router.get("", response_model=list[DealOut])
async def list_deals(status: str | None = Query(default=None, pattern="^(open|won|lost)$"), stage: str | None = None,
                     owner_id: int | None = None, contact_id: int | None = None, conversation_id: int | None = None,
                     q: str | None = None, pipeline: str | None = None, limit: int = Query(default=200, le=500),
                     agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    stmt = _filtered(agent.organization_id, status, stage, owner_id, contact_id, q, pipeline)
    if conversation_id:
        stmt = stmt.where(Deal.conversation_id == conversation_id)
    rows = (await session.scalars(stmt.order_by(Deal.updated_at.desc()).limit(limit))).unique().all()
    return [await deal_out(session, d) for d in rows]


@router.get("/board")
async def board(pipeline: str = "default", owner_id: int | None = None, q: str | None = None,
                agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    config = await pl.get_pipelines(session, agent.organization_id)
    p = config["pipelines"].get(pipeline)
    if not p:
        raise HTTPException(404, "Embudo no encontrado")
    rows = (await session.scalars(_filtered(agent.organization_id, None, None, owner_id, None, q, pipeline)
                                  .order_by(Deal.updated_at.desc()).limit(1000))).unique().all()
    columns = {s["key"]: [] for s in p["stages"]}
    totals = {s["key"]: 0.0 for s in p["stages"]}
    for d in rows:
        if d.stage in columns:
            columns[d.stage].append((await deal_out(session, d)).model_dump(mode="json"))
            totals[d.stage] += float(d.amount or 0)
    return {"pipeline": pipeline, "label": p.get("label", pipeline), "stages": p["stages"], "columns": columns,
            "totals": totals, "currency": config.get("currency", "COP")}


# --- CRUD -----------------------------------------------------------------------
@router.post("", response_model=DealOut)
async def create_deal(body: DealIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    data = body.model_dump()
    await _validate_refs(session, agent.organization_id, data)
    config = await pl.get_pipelines(session, agent.organization_id)
    stages = pl.stage_keys(config, body.pipeline)
    if not stages:
        raise HTTPException(422, "Embudo inválido")
    deal = Deal(organization_id=agent.organization_id, contact_id=body.contact_id, conversation_id=body.conversation_id,
                owner_agent_id=body.owner_agent_id or agent.id, name=body.name.strip(), amount=body.amount,
                currency=(body.currency or config.get("currency") or "COP").upper()[:3], pipeline=body.pipeline,
                stage=stages[0], expected_close=body.expected_close, source="agent")
    session.add(deal)
    event = _apply_stage(deal, body.stage or stages[0], stages)
    await session.flush()
    if event:
        await _log_event(session, deal, event, agent.id)
    await session.commit()
    await session.refresh(deal, ["contact", "owner"])
    return await deal_out(session, deal)


@router.get("/{deal_id}", response_model=DealOut)
async def get_deal(deal_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await deal_out(session, await _deal(session, deal_id, agent.organization_id))


@router.put("/{deal_id}", response_model=DealOut)
async def update_deal(deal_id: int, body: DealUpdate, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, deal_id, agent.organization_id)
    data = body.model_dump(exclude_unset=True)
    await _validate_refs(session, agent.organization_id, data)
    config = await pl.get_pipelines(session, agent.organization_id)
    stage = data.pop("stage", None)
    for k, v in data.items():
        if k == "name":
            v = v.strip()
        if k == "currency" and v:
            v = v.upper()[:3]
        setattr(deal, k, v)
    event = _apply_stage(deal, stage, pl.stage_keys(config, deal.pipeline)) if stage else None
    if event:
        await _log_event(session, deal, event, agent.id)
    await session.commit()
    await session.refresh(deal)
    await session.refresh(deal, ["contact", "owner"])
    return await deal_out(session, deal)


@router.post("/{deal_id}/win", response_model=DealOut)
async def win(deal_id: int, body: WinIn | None = None, agent: Agent = Depends(current_agent),
              session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, deal_id, agent.organization_id)
    if body and body.amount is not None:
        if body.amount < 0:
            raise HTTPException(422, "El monto no puede ser negativo")
        deal.amount = body.amount
    config = await pl.get_pipelines(session, agent.organization_id)
    event = _apply_stage(deal, "won", pl.stage_keys(config, deal.pipeline))
    if event:
        await _log_event(session, deal, event, agent.id)
    await session.commit()
    await session.refresh(deal)
    await session.refresh(deal, ["contact", "owner"])
    return await deal_out(session, deal)


@router.post("/{deal_id}/lose", response_model=DealOut)
async def lose(deal_id: int, body: LoseIn | None = None, agent: Agent = Depends(current_agent),
               session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, deal_id, agent.organization_id)
    config = await pl.get_pipelines(session, agent.organization_id)
    event = _apply_stage(deal, "lost", pl.stage_keys(config, deal.pipeline))
    deal.lost_reason = (body.reason if body else None) or deal.lost_reason
    if event:
        await _log_event(session, deal, event, agent.id)
    await session.commit()
    await session.refresh(deal)
    await session.refresh(deal, ["contact", "owner"])
    return await deal_out(session, deal)


@router.post("/{deal_id}/reopen", response_model=DealOut)
async def reopen(deal_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, deal_id, agent.organization_id)
    config = await pl.get_pipelines(session, agent.organization_id)
    open_stages = [s for s in pl.stage_keys(config, deal.pipeline) if s not in pl.CLOSED_STAGES]
    _apply_stage(deal, open_stages[-1] if open_stages else "new", pl.stage_keys(config, deal.pipeline))
    await session.commit()
    await session.refresh(deal)
    await session.refresh(deal, ["contact", "owner"])
    return await deal_out(session, deal)


@router.delete("/{deal_id}")
async def delete_deal(deal_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, deal_id, agent.organization_id)
    await session.delete(deal)
    await session.commit()
    return {"ok": True}
