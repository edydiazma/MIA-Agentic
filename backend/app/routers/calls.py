"""Llamadas: historial, detalle, control desde el panel (contestar, transferir, colgar), grabación y métricas."""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import current_agent
from app.db import get_session, set_actor
from app.models import Agent, Call, CallEvent, CallTurn, Contact
from app.voice import calls as signaling
from app.voice.session import registry

router = APIRouter(prefix="/api/calls", tags=["calls"])


class SdpIn(BaseModel):
    sdp: str


async def _call(session: AsyncSession, call_id: int, org: int) -> Call:
    call = await session.get(Call, call_id)
    if not call or call.organization_id != org:
        raise HTTPException(404, "Llamada no encontrada")
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    return call


@router.get("")
async def list_calls(
    status: str | None = None, handled_by: str | None = None, channel_id: int | None = None, q: str | None = None,
    start: date | None = None, end: date | None = None, limit: int = Query(default=50, le=200), offset: int = 0,
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    stmt = select(Call).join(Contact, Contact.id == Call.contact_id).where(Call.organization_id == agent.organization_id)
    if status:
        stmt = stmt.where(Call.status == status)
    if handled_by:
        stmt = stmt.where(Call.handled_by == handled_by)
    if channel_id:
        stmt = stmt.where(Call.channel_id == channel_id)
    if q:
        stmt = stmt.where(or_(Contact.name.ilike(f"%{q}%"), Contact.wa_id.ilike(f"%{q}%")))
    if start:
        stmt = stmt.where(Call.started_at >= datetime.combine(start, datetime.min.time(), UTC))
    if end:
        stmt = stmt.where(Call.started_at < datetime.combine(end, datetime.min.time(), UTC) + timedelta(days=1))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(Call.started_at.desc()).offset(offset).limit(limit))).unique().all()
    return {"total": total, "items": [signaling.call_out(c) for c in rows]}


@router.get("/active")
async def active_calls(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Llamadas que suenan o están en curso (para reconstruir el estado al recargar el panel)."""
    rows = (await session.scalars(select(Call).where(
        Call.organization_id == agent.organization_id, Call.status.in_(signaling.ACTIVE)))).unique().all()
    out = []
    for c in rows:
        item = signaling.call_out(c)
        if c.status == "ringing":
            ev = await session.scalar(select(CallEvent).where(CallEvent.call_id == c.id, CallEvent.type == "connect"))
            item["sdp_offer"] = (ev.payload or {}).get("sdp_offer") if ev else None
            item["mode"] = "answer"
        elif c.status == "transferring":
            item["mode"] = "bridge"
        out.append(item)
    return out


@router.get("/stats")
async def stats(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                session: AsyncSession = Depends(get_session)):
    from app.plans import check_limit

    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=29)
    lo = datetime.combine(start, datetime.min.time(), UTC)
    hi = datetime.combine(end, datetime.min.time(), UTC) + timedelta(days=1)
    base = (Call.organization_id == agent.organization_id, Call.started_at >= lo, Call.started_at < hi)
    by_status = dict((await session.execute(select(Call.status, func.count()).where(*base)
                                            .group_by(Call.status))).all())
    by_handler = dict((await session.execute(select(Call.handled_by, func.count()).where(*base, Call.handled_by.is_not(None))
                                             .group_by(Call.handled_by))).all())
    totals = (await session.execute(select(func.count(), func.count(Call.answered_at), func.avg(Call.duration_s),
                                           func.coalesce(func.sum(Call.duration_s), 0)).where(*base))).one()
    daily = (await session.execute(select(func.date(Call.started_at), func.count(), func.count(Call.answered_at))
                                   .where(*base).group_by(func.date(Call.started_at))
                                   .order_by(func.date(Call.started_at)))).all()
    minutes = await check_limit(session, agent.organization_id, "voice_minutes_month")
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "total": totals[0], "answered": totals[1],
        "answer_rate": round(100 * totals[1] / totals[0], 1) if totals[0] else None,
        "avg_duration_s": round(float(totals[2]), 1) if totals[2] is not None else None,
        "talk_minutes": round(int(totals[3]) / 60, 1),
        "by_status": by_status, "by_handler": by_handler,
        "daily": [{"day": d.isoformat(), "calls": n, "answered": a} for d, n, a in daily],
        "minutes_month": minutes,
    }


@router.get("/{call_id}")
async def get_call(call_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    call = await _call(session, call_id, agent.organization_id)
    turns = (await session.scalars(select(CallTurn).where(CallTurn.call_id == call.id).order_by(CallTurn.id))).all()
    events = (await session.scalars(select(CallEvent).where(CallEvent.call_id == call.id)
                                    .order_by(CallEvent.created_at))).all()
    return {**signaling.call_out(call), "transcript": call.transcript,
            "turns": [{"speaker": t.speaker, "text": t.text, "start_ms": t.start_ms, "created_at": t.created_at}
                      for t in turns],
            "events": [{"type": e.type, "created_at": e.created_at,
                        "payload": {k: v for k, v in (e.payload or {}).items() if k != "sdp_offer"}} for e in events]}


@router.post("/{call_id}/answer")
async def answer(call_id: int, body: SdpIn, agent: Agent = Depends(current_agent),
                 session: AsyncSession = Depends(get_session)):
    """El navegador del asesor envía su SDP answer; el primero que llega se queda con la llamada."""
    call = await _call(session, call_id, agent.organization_id)
    await set_actor(session, "agent", agent.id)
    try:
        call = await signaling.answer_by_agent(session, call, agent, body.sdp)
    except signaling.CallTaken:
        raise HTTPException(409, "Otro asesor ya contestó esta llamada") from None
    return signaling.call_out(call)


@router.post("/{call_id}/bridge")
async def bridge(call_id: int, body: SdpIn, agent: Agent = Depends(current_agent),
                 session: AsyncSession = Depends(get_session)):
    """Transferencia desde el agente de voz: el navegador envía una oferta y el servidor puentea el audio."""
    call = await _call(session, call_id, agent.organization_id)
    media = registry.get(call.id)
    if not media:
        raise HTTPException(409, "La llamada ya no está activa")
    won = await session.execute(update(Call).where(Call.id == call.id, Call.status == "transferring")
                                .values(status="connected", handled_by="agent", agent_id=agent.id).returning(Call.id))
    if not won.first():
        await session.rollback()
        raise HTTPException(409, "Otro asesor ya tomó esta llamada")
    await signaling.log_event(session, call.id, "bridge", {"agent_id": agent.id})
    await session.commit()
    answer_sdp = await media.bridge_to_agent(body.sdp)
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    await signaling.broadcast_call(call, "call.taken")
    await signaling.broadcast_call(call)
    return {"sdp": answer_sdp, "call": signaling.call_out(call)}


@router.post("/{call_id}/reject")
async def reject(call_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    call = await _call(session, call_id, agent.organization_id)
    if call.status != "ringing":
        raise HTTPException(409, "La llamada ya no está sonando")
    return signaling.call_out(await signaling.reject_call(session, call, f"Rechazada por {agent.name}"))


@router.post("/{call_id}/hangup")
async def hangup(call_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    call = await _call(session, call_id, agent.organization_id)
    if call.status not in signaling.ACTIVE:
        return signaling.call_out(call)
    return signaling.call_out(await signaling.hangup(session, call, f"Colgada por {agent.name}"))


@router.get("/{call_id}/recording")
async def recording(call_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    call = await _call(session, call_id, agent.organization_id)
    if not call.recording_path:
        raise HTTPException(404, "La llamada no tiene grabación")
    data = await storage.download(call.recording_path)
    return Response(content=data, media_type="audio/wav",
                    headers={"Content-Disposition": f'inline; filename="llamada-{call.id}.wav"'})

