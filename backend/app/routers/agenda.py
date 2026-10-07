"""Seguimiento (tareas de asesores sobre clientes) y Citas."""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import appointments as appt_svc
from app.auth import current_agent
from app.db import get_session
from app.models import Agent, Appointment, Contact, Conversation, FollowUp, utcnow
from app.realtime import hub

router = APIRouter(prefix="/api", tags=["agenda"])

APPT_STATUSES = ("scheduled", "done", "cancelled", "no_show")


class FollowUpIn(BaseModel):
    contact_id: int
    conversation_id: int | None = None
    due_at: datetime
    note: str
    agent_id: int | None = None


class FollowUpUpdate(BaseModel):
    due_at: datetime | None = None
    note: str | None = None
    done: bool | None = None
    agent_id: int | None = None


class AppointmentIn(BaseModel):
    contact_id: int
    conversation_id: int | None = None
    starts_at: datetime
    duration_min: int | None = None
    title: str | None = None
    notes: str | None = None
    agent_id: int | None = None


class AppointmentUpdate(BaseModel):
    starts_at: datetime | None = None
    status: str | None = None
    notes: str | None = None
    agent_id: int | None = None


def _utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _fu(f: FollowUp) -> dict:
    done = f.done_at is not None
    return {
        "id": f.id, "contact_id": f.contact_id, "contact_name": f.contact.name, "wa_id": f.contact.wa_id,
        "conversation_id": f.conversation_id, "agent_id": f.agent_id, "agent_name": f.agent.name,
        "due_at": _utc(f.due_at).isoformat(), "note": f.note, "done": done,
        "done_at": _utc(f.done_at).isoformat() if f.done_at else None,
        "overdue": not done and _utc(f.due_at) < utcnow(),
    }


def _ap(a: Appointment) -> dict:
    return {
        "id": a.id, "contact_id": a.contact_id, "contact_name": a.contact.name, "wa_id": a.contact.wa_id,
        "conversation_id": a.conversation_id, "agent_id": a.agent_id, "agent_name": a.agent.name if a.agent else None,
        "starts_at": _utc(a.starts_at).isoformat(), "duration_min": a.duration_min, "title": a.title,
        "notes": a.notes, "status": a.status, "created_by": a.created_by_type,
    }


async def _check_refs(session: AsyncSession, org: int, contact_id: int, conversation_id: int | None,
                      agent_id: int | None) -> None:
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != org:
        raise HTTPException(404, "Contacto no encontrado")
    if conversation_id:
        conv = await session.get(Conversation, conversation_id)
        if not conv or conv.organization_id != org or conv.contact_id != contact_id:
            raise HTTPException(422, "La conversación no corresponde al contacto")
    if agent_id:
        a = await session.get(Agent, agent_id)
        if not a or a.organization_id != org:
            raise HTTPException(404, "Asesor no encontrado")


# --- Seguimiento ----------------------------------------------------------------
@router.get("/followups")
async def list_followups(
    scope: str = Query(default="mine", pattern="^(mine|all)$"),
    status: str = Query(default="pending", pattern="^(pending|done|all)$"),
    contact_id: int | None = None,
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    stmt = select(FollowUp).where(FollowUp.organization_id == agent.organization_id).order_by(FollowUp.due_at)
    if scope == "mine":
        stmt = stmt.where(FollowUp.agent_id == agent.id)
    if status == "pending":
        stmt = stmt.where(FollowUp.done_at.is_(None))
    elif status == "done":
        stmt = stmt.where(FollowUp.done_at.is_not(None))
    if contact_id:
        stmt = stmt.where(FollowUp.contact_id == contact_id)
    return [_fu(f) for f in (await session.scalars(stmt.limit(500))).unique().all()]


@router.post("/followups")
async def create_followup(body: FollowUpIn, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    await _check_refs(session, org, body.contact_id, body.conversation_id, body.agent_id)
    if not body.note.strip():
        raise HTTPException(422, "Escribe la nota del seguimiento")
    f = FollowUp(organization_id=org, contact_id=body.contact_id, conversation_id=body.conversation_id,
                 agent_id=body.agent_id or agent.id, due_at=_utc(body.due_at), note=body.note.strip())
    session.add(f)
    await session.commit()
    await session.refresh(f, ["contact", "agent"])
    return _fu(f)


async def _followup(session: AsyncSession, fid: int, agent: Agent) -> FollowUp:
    f = await session.get(FollowUp, fid)
    if not f or f.organization_id != agent.organization_id:
        raise HTTPException(404, "Seguimiento no encontrado")
    return f


@router.put("/followups/{fid}")
async def update_followup(fid: int, body: FollowUpUpdate, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    f = await _followup(session, fid, agent)
    data = body.model_dump(exclude_unset=True)
    if "done" in data:
        f.done_at = (f.done_at or utcnow()) if data.pop("done") else None
    if data.get("due_at"):
        data["due_at"] = _utc(data["due_at"])
    if data.get("agent_id"):
        await _check_refs(session, agent.organization_id, f.contact_id, None, data["agent_id"])
    for k, v in data.items():
        setattr(f, k, v)
    await session.commit()
    await session.refresh(f, ["contact", "agent"])
    return _fu(f)


@router.delete("/followups/{fid}")
async def delete_followup(fid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await session.delete(await _followup(session, fid, agent))
    await session.commit()
    return {"ok": True}


# --- Citas ----------------------------------------------------------------------
@router.get("/appointments")
async def list_appointments(
    start: date | None = None, end: date | None = None, contact_id: int | None = None,
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    org = agent.organization_id
    _, tz = await appt_svc.config(session, org)
    start = start or utcnow().astimezone(tz).date()
    end = end or start + timedelta(days=30)
    lo = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    hi = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    stmt = (select(Appointment).where(Appointment.organization_id == org, Appointment.starts_at >= lo,
                                      Appointment.starts_at < hi).order_by(Appointment.starts_at))
    if contact_id:
        stmt = stmt.where(Appointment.contact_id == contact_id)
    return [_ap(a) for a in (await session.scalars(stmt)).unique().all()]


@router.get("/appointments/availability")
async def availability(day: date, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return [s.isoformat() for s in await appt_svc.available_slots(session, agent.organization_id, day)]


@router.post("/appointments")
async def create_appointment(body: AppointmentIn, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    await _check_refs(session, org, body.contact_id, body.conversation_id, body.agent_id)
    cfg, _tz = await appt_svc.config(session, org)
    a = Appointment(organization_id=org, contact_id=body.contact_id, conversation_id=body.conversation_id,
                    agent_id=body.agent_id, starts_at=_utc(body.starts_at),
                    duration_min=body.duration_min or cfg["duration_min"], title=body.title or cfg["title"],
                    notes=body.notes, created_by_type="agent")
    session.add(a)
    await session.commit()
    await session.refresh(a, ["contact", "agent"])
    out = _ap(a)
    await hub.broadcast("appointment.created", out, org)
    return out


@router.put("/appointments/{aid}")
async def update_appointment(aid: int, body: AppointmentUpdate, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    a = await session.get(Appointment, aid)
    if not a or a.organization_id != agent.organization_id:
        raise HTTPException(404, "Cita no encontrada")
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in APPT_STATUSES:
        raise HTTPException(422, "Estado inválido")
    if data.get("starts_at"):
        data["starts_at"] = _utc(data["starts_at"])
    if data.get("agent_id"):
        await _check_refs(session, agent.organization_id, a.contact_id, None, data["agent_id"])
    for k, v in data.items():
        setattr(a, k, v)
    await session.commit()
    await session.refresh(a, ["contact", "agent"])
    return _ap(a)
