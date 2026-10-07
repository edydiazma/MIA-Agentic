"""Estados de asesor: catálogo (admin), cambio de estado propio o por supervisor, latido, salida y tablero de estados."""

import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import statuses
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, AgentGroup, AgentSession, AgentStatus, AgentStatusEvent, Conversation, utcnow
from app.realtime import hub

router = APIRouter(prefix="/api", tags=["agent-status"])


class StatusIn(BaseModel):
    key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: str = Field(min_length=1, max_length=60)
    icon: str | None = None
    color: str | None = None
    receives_conversations: bool = False
    counts_as_working: bool = True
    is_default: bool = False
    is_offline: bool = False
    position: int = 100
    is_active: bool = True


class SetStatusIn(BaseModel):
    status_id: int


def _slug(name: str) -> str:
    """Clave a partir del nombre: sin tildes, minúsculas, guion bajo ("Capacitación" → "capacitacion")."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name.lower()) if not unicodedata.combining(c))
    key = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")[:38] or "estado"
    return key if key[0].isalpha() else f"e_{key}"


async def _status(session: AsyncSession, org: int, sid: int) -> AgentStatus:
    s = await session.get(AgentStatus, sid)
    if not s or s.organization_id != org:
        raise HTTPException(404, "Estado no encontrado")
    return s


async def _apply_flags(session: AsyncSession, org: int, row: AgentStatus, body: StatusIn) -> None:
    """Un solo estado por defecto y uno de desconexión por empresa."""
    if body.is_default and body.is_offline:
        raise HTTPException(422, "Un estado no puede ser el inicial y el de desconexión a la vez")
    if body.is_default and body.is_offline is False and not body.is_active:
        raise HTTPException(422, "El estado inicial debe estar activo")
    others = (await session.scalars(select(AgentStatus).where(AgentStatus.organization_id == org,
                                                              AgentStatus.id != (row.id or 0)))).all()
    if body.is_default:
        for o in others:
            o.is_default = False
    if body.is_offline:
        for o in others:
            o.is_offline = False
    await session.flush()


@router.get("/agent-statuses")
async def list_statuses(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = await statuses.ensure_statuses(session, agent.organization_id)
    await session.commit()
    return [statuses.status_out(r) for r in rows]


@router.post("/agent-statuses")
async def create_status(body: StatusIn, admin: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    await statuses.ensure_statuses(session, org)
    key = body.key or _slug(body.name)
    if await session.scalar(select(AgentStatus.id).where(AgentStatus.organization_id == org, AgentStatus.key == key)):
        raise HTTPException(409, "Ya existe un estado con esa clave")
    row = AgentStatus(organization_id=org, key=key)
    await _apply_flags(session, org, row, body)
    for f in ("name", "icon", "color", "receives_conversations", "counts_as_working", "is_default", "is_offline",
              "position", "is_active"):
        setattr(row, f, getattr(body, f))
    session.add(row)
    await session.commit()
    return statuses.status_out(row)


@router.put("/agent-statuses/{sid}")
async def update_status(sid: int, body: StatusIn, admin: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    row = await _status(session, admin.organization_id, sid)
    if row.key in statuses.SYSTEM_KEYS and not body.is_active:
        raise HTTPException(422, "Los estados del sistema no se pueden desactivar")
    if row.key == "offline" and body.receives_conversations:
        raise HTTPException(422, "El estado desconectado no puede recibir conversaciones")
    await _apply_flags(session, admin.organization_id, row, body)
    for f in ("name", "icon", "color", "receives_conversations", "counts_as_working", "is_default", "is_offline",
              "position", "is_active"):
        setattr(row, f, getattr(body, f))
    # Los asesores en este estado heredan la nueva disponibilidad
    for a in (await session.scalars(select(Agent).where(Agent.status_id == row.id))).all():
        a.availability = statuses.availability_for(row)
    await session.commit()
    return statuses.status_out(row)


@router.delete("/agent-statuses/{sid}")
async def delete_status(sid: int, admin: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    row = await _status(session, admin.organization_id, sid)
    if row.key in statuses.SYSTEM_KEYS or row.is_default or row.is_offline:
        raise HTTPException(422, "Los estados del sistema, el inicial y el de desconexión no se pueden eliminar")
    if await session.scalar(select(func.count()).select_from(Agent).where(Agent.status_id == row.id)):
        raise HTTPException(409, "Hay asesores en este estado: cámbialos antes de eliminarlo (o desactívalo)")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/me/status")
async def my_status(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    s = await statuses.current_status(session, agent)
    return {"status": statuses.status_out(s), "since": agent.status_changed_at, "availability": agent.availability}


@router.put("/me/status")
async def set_my_status(body: SetStatusIn, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    s = await _status(session, agent.organization_id, body.status_id)
    if not s.is_active:
        raise HTTPException(422, "Estado inactivo")
    await statuses.set_status(session, agent, s, "manual")
    return {"status": statuses.status_out(s), "since": agent.status_changed_at, "availability": agent.availability}


@router.put("/agents/{agent_id}/status")
async def set_agent_status(agent_id: int, body: SetStatusIn, me: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    """Un supervisor (de un grupo del asesor) o un administrador cambia el estado de otro asesor."""
    if me.role not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo supervisores y administradores")
    target = await session.get(Agent, agent_id)
    if not target or target.organization_id != me.organization_id:
        raise HTTPException(404, "Asesor no encontrado")
    if me.role == "supervisor" and me.id != target.id:
        mine = select(AgentGroup.group_id).where(AgentGroup.agent_id == me.id)
        shared = await session.scalar(select(func.count()).select_from(AgentGroup).where(
            AgentGroup.agent_id == target.id, AgentGroup.group_id.in_(mine)))
        if not shared:
            raise HTTPException(403, "El asesor no está en tus grupos")
    s = await _status(session, me.organization_id, body.status_id)
    await statuses.set_status(session, target, s, "supervisor")
    return {"agent_id": target.id, "status": statuses.status_out(s), "since": target.status_changed_at}


@router.post("/me/heartbeat")
async def heartbeat(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """El panel lo llama cada minuto: mantiene viva la sesión (si no, pasa a desconectado)."""
    await statuses.heartbeat(session, agent)
    return {"ok": True}


@router.post("/me/logout")
async def logout(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await statuses.on_logout(session, agent)
    return {"ok": True}


@router.get("/agents/status-board")
async def status_board(me: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Asesores con estado, desde cuándo, tiempo en el estado, conversaciones abiertas y sesión (tablero en vivo)."""
    org = me.organization_id
    rows = await statuses.ensure_statuses(session, org)
    by_id = {r.id: r for r in rows}
    by_key = {r.key: r for r in rows}
    stmt = select(Agent).where(Agent.organization_id == org, Agent.is_active).order_by(Agent.name)
    if me.role == "supervisor":  # supervisor: los asesores de sus grupos
        mine = select(AgentGroup.group_id).where(AgentGroup.agent_id == me.id)
        stmt = stmt.where(Agent.id.in_(select(AgentGroup.agent_id).where(AgentGroup.group_id.in_(mine))) |
                          (Agent.id == me.id))
    elif me.role != "admin":
        stmt = stmt.where(Agent.id == me.id)
    agents = (await session.scalars(stmt)).all()
    ids = [a.id for a in agents]
    open_counts = dict((await session.execute(
        select(Conversation.assigned_agent_id, func.count()).where(
            Conversation.status == "human", Conversation.assigned_agent_id.in_(ids or [0]))
        .group_by(Conversation.assigned_agent_id))).all())
    sessions = dict((await session.execute(
        select(AgentSession.agent_id, func.max(AgentSession.started_at)).where(
            AgentSession.agent_id.in_(ids or [0]), AgentSession.ended_at.is_(None))
        .group_by(AgentSession.agent_id))).all())
    open_since = dict((await session.execute(
        select(AgentStatusEvent.agent_id, AgentStatusEvent.started_at).where(
            AgentStatusEvent.agent_id.in_(ids or [0]), AgentStatusEvent.ended_at.is_(None)))).all())
    online = hub.online_agent_ids(org)
    now = utcnow()
    out = []
    for a in agents:
        st = by_id.get(a.status_id) or by_key.get(a.availability)
        since = open_since.get(a.id) or a.status_changed_at
        out.append({"agent_id": a.id, "name": a.name, "role": a.role, "employee_code": a.employee_code,
                    "status": statuses.status_out(st), "since": since,
                    "seconds_in_status": int((now - since).total_seconds()) if since else None,
                    "online": a.id in online, "open_conversations": open_counts.get(a.id, 0),
                    "session_started_at": sessions.get(a.id)})
    return out
