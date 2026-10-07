"""Estados de asesor, bitácora de tiempo por estado y sesiones de ingreso. docs/data-model.md §18.1

Cada asesor tiene a lo sumo un tramo abierto en agent_status_events (índice único): cambiar de estado cierra el
tramo (duration_s) y abre otro en la misma transacción. agents.availability se mantiene sincronizado
(recibe conversaciones → available; desconectado o que no cuenta como trabajo → away; si no → busy) para que el
enrutamiento y la presencia existentes sigan funcionando.
"""

import asyncio
import logging
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Agent, AgentSession, AgentStatus, AgentStatusEvent, utcnow
from app.realtime import hub
from app.settings_store import get_setting

log = logging.getLogger(__name__)

SYSTEM_KEYS = ("available", "busy", "away", "offline")
_SYSTEM_ROWS = [
    ("available", "Disponible", "🟢", "#16a34a", True, True, True, False, 10),
    ("busy", "Ocupado", "🔴", "#dc2626", False, True, False, False, 20),
    ("away", "Ausente", "🟡", "#ca8a04", False, False, False, False, 30),
    ("offline", "Desconectado", "⚪", "#9ca3af", False, False, False, True, 90),
]
_tasks: set[asyncio.Task] = set()


def availability_for(status: AgentStatus) -> str:
    if status.receives_conversations:
        return "available"
    if status.is_offline or not status.counts_as_working:
        return "away"
    return "busy"


def status_out(s: AgentStatus | None) -> dict | None:
    if s is None:
        return None
    return {"id": s.id, "key": s.key, "name": s.name, "icon": s.icon, "color": s.color,
            "receives_conversations": s.receives_conversations, "counts_as_working": s.counts_as_working,
            "is_default": s.is_default, "is_offline": s.is_offline, "position": s.position, "is_active": s.is_active,
            "is_system": s.key in SYSTEM_KEYS}


async def ensure_statuses(session: AsyncSession, org: int) -> list[AgentStatus]:
    """Estados de la empresa (crea los del sistema si faltan: empresas anteriores al trigger)."""
    rows = list((await session.scalars(select(AgentStatus).where(AgentStatus.organization_id == org)
                                       .order_by(AgentStatus.position, AgentStatus.id))).all())
    have = {r.key for r in rows}
    missing = [r for r in _SYSTEM_ROWS if r[0] not in have]
    if missing:
        for key, name, icon, color, recv, work, default, offline, pos in missing:
            session.add(AgentStatus(organization_id=org, key=key, name=name, icon=icon, color=color,
                                    receives_conversations=recv, counts_as_working=work,
                                    is_default=default and not any(r.is_default and r.is_active for r in rows),
                                    is_offline=offline and not any(r.is_offline and r.is_active for r in rows),
                                    position=pos))
        await session.flush()
        rows = list((await session.scalars(select(AgentStatus).where(AgentStatus.organization_id == org)
                                           .order_by(AgentStatus.position, AgentStatus.id))).all())
    return rows


async def _special(session: AsyncSession, org: int, which: str) -> AgentStatus | None:
    rows = await ensure_statuses(session, org)
    active = [r for r in rows if r.is_active]
    if which == "default":
        return next((r for r in active if r.is_default), None) or next((r for r in active if r.key == "available"), None)
    return next((r for r in active if r.is_offline), None) or next((r for r in active if r.key == "offline"), None)


async def current_status(session: AsyncSession, agent: Agent) -> AgentStatus | None:
    if agent.status_id:
        s = await session.get(AgentStatus, agent.status_id)
        if s and s.organization_id == agent.organization_id:
            return s
    # Asesor sin estado (anterior a la migración): el equivalente a su disponibilidad
    rows = await ensure_statuses(session, agent.organization_id)
    return next((r for r in rows if r.key == agent.availability), None)


async def set_status(session: AsyncSession, agent: Agent, status: AgentStatus, source: str = "manual",
                     commit: bool = True) -> AgentStatusEvent:
    """Cierra el tramo abierto y abre uno nuevo (atómico). No hace nada si ya está en ese estado."""
    now = utcnow()
    open_ev = (await session.scalars(select(AgentStatusEvent).where(
        AgentStatusEvent.agent_id == agent.id, AgentStatusEvent.ended_at.is_(None)).with_for_update())).first()
    if open_ev is not None and open_ev.status_id == status.id and agent.status_id == status.id:
        return open_ev
    if open_ev is not None:
        open_ev.ended_at = now
        open_ev.duration_s = max(0, int((now - open_ev.started_at).total_seconds()))
        await session.flush()  # libera el índice único del tramo abierto
    ev = AgentStatusEvent(organization_id=agent.organization_id, agent_id=agent.id, status_id=status.id,
                          status_key=status.key, started_at=now, source=source)
    session.add(ev)
    agent.status_id, agent.status_changed_at = status.id, now
    agent.availability = availability_for(status)
    if commit:
        await session.commit()
    else:
        await session.flush()
    await hub.broadcast("agent.status", {"agent_id": agent.id, "status": status_out(status),
                                         "availability": agent.availability, "since": now.isoformat(),
                                         "source": source}, agent.organization_id)
    return ev


# --- Sesiones ------------------------------------------------------------------------------------------
def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


async def on_login(session: AsyncSession, agent: Agent, request: Request | None = None) -> None:
    """Ingreso: abre la sesión (cierra las anteriores) y pone el estado por defecto si estaba desconectado."""
    try:
        now = utcnow()
        await session.execute(update(AgentSession).where(
            AgentSession.agent_id == agent.id, AgentSession.ended_at.is_(None)).values(ended_at=now, end_reason="replaced"))
        session.add(AgentSession(organization_id=agent.organization_id, agent_id=agent.id, started_at=now,
                                 last_seen_at=now, ip=_client_ip(request),
                                 user_agent=(request.headers.get("user-agent") if request else None)))
        current = await current_status(session, agent)
        if current is None or current.is_offline or agent.status_id is None:
            default = await _special(session, agent.organization_id, "default")
            if default:
                await set_status(session, agent, default, "login", commit=False)
        await session.commit()
    except Exception:  # el ingreso nunca falla por la bitácora
        log.exception("No se pudo registrar el ingreso del asesor %s", agent.id)
        await session.rollback()


async def on_logout(session: AsyncSession, agent: Agent, request: Request | None = None, reason: str = "logout") -> None:
    """Salida (o expiración / desactivación): cierra la sesión y pasa al estado de desconexión."""
    del request  # firma común con on_login (el gancho de /auth/logout pasa la petición)
    now = utcnow()
    await session.execute(update(AgentSession).where(
        AgentSession.agent_id == agent.id, AgentSession.ended_at.is_(None)).values(ended_at=now, end_reason=reason))
    offline = await _special(session, agent.organization_id, "offline")
    if offline:
        await set_status(session, agent, offline, "logout" if reason == "logout" else "idle", commit=False)
    await session.commit()


async def heartbeat(session: AsyncSession, agent: Agent) -> None:
    now = utcnow()
    agent.last_seen_at = now
    res = await session.execute(update(AgentSession).where(
        AgentSession.agent_id == agent.id, AgentSession.ended_at.is_(None)).values(last_seen_at=now))
    if not res.rowcount:  # sesión expirada o de antes de la migración: se abre una nueva
        session.add(AgentSession(organization_id=agent.organization_id, agent_id=agent.id, started_at=now,
                                 last_seen_at=now))
        current = await current_status(session, agent)
        if current is None or current.is_offline:
            default = await _special(session, agent.organization_id, "default")
            if default:
                await set_status(session, agent, default, "login", commit=False)
    await session.commit()


async def expire_sessions() -> int:
    """Sesiones sin latido por más de session_timeout_minutes y sin pestañas abiertas → desconectado."""
    closed = 0
    async with SessionLocal() as session:
        rows = (await session.execute(select(AgentSession.organization_id).where(
            AgentSession.ended_at.is_(None)).distinct())).scalars().all()
        for org in rows:
            cfg = await get_setting(session, "routing", org)
            cutoff = utcnow() - timedelta(minutes=float(cfg.get("session_timeout_minutes") or 15))
            stale = (await session.scalars(select(AgentSession).where(
                AgentSession.organization_id == org, AgentSession.ended_at.is_(None),
                AgentSession.last_seen_at < cutoff))).all()
            online = hub.online_agent_ids(org)
            for s in stale:
                if s.agent_id in online:
                    s.last_seen_at = utcnow()
                    continue
                agent = await session.get(Agent, s.agent_id)
                if agent is None:
                    continue
                await on_logout(session, agent, reason="timeout")
                closed += 1
            await session.commit()
    return closed


def on_ws_disconnect(agent_id: int, organization_id: int) -> None:
    """Tras cerrar la última pestaña espera offline_grace_seconds; si no volvió, queda desconectado."""
    async def job():
        try:
            async with SessionLocal() as session:
                cfg = await get_setting(session, "routing", organization_id)
            await asyncio.sleep(float(cfg.get("offline_grace_seconds") or 120))
            if agent_id in hub.online_agent_ids(organization_id):
                return
            async with SessionLocal() as session:
                agent = await session.get(Agent, agent_id)
                if agent is not None and agent.is_active:
                    await on_logout(session, agent, reason="timeout")
        except Exception:
            log.exception("No se pudo cerrar la sesión del asesor %s", agent_id)

    try:
        task = asyncio.get_running_loop().create_task(job())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def sessions_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            n = await expire_sessions()
            if n:
                log.info("Sesiones expiradas: %s", n)
        except Exception:
            log.exception("Falló la expiración de sesiones")
