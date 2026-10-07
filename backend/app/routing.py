"""Enrutamiento de conversaciones a asesores. docs/data-model.md §18.1

Elegibles: activos, conectados al panel y con un estado que recibe conversaciones (agents.availability =
'available', que sincroniza app.statuses), del grupo si lo hay y por debajo de max_open_per_agent.
Prioridad: dueño del cliente (routing sticky_owner) → mismo asesor de la vez anterior (ajuste sticky_agent) →
estrategia del grupo (least_loaded | round_robin; manual = no asigna). Si nadie está disponible y el horario está
abierto, el ajuste assign_when_none_available asigna a un integrante no disponible; si no, queda en cola.
"""

import logging

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentGroup, Contact, Conversation, Group, utcnow
from app.realtime import hub
from app.settings_store import get_setting

log = logging.getLogger(__name__)


async def _notify(session: AsyncSession, agent_id: int, type_: str, title: str, body: str | None = None,
                  link: str | None = None, data: dict | None = None) -> None:
    """Notificación del panel (módulo de productividad); nunca falla el flujo principal."""
    try:
        from app.notifications import notify
    except (ImportError, AttributeError):
        return
    try:
        await notify(session, agent_id, type_, title, body, link, data)
    except Exception:
        log.debug("No se pudo crear la notificación", exc_info=True)


async def _load(session: AsyncSession, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return dict((await session.execute(
        select(Conversation.assigned_agent_id, func.count())
        .where(Conversation.status == "human", Conversation.assigned_agent_id.in_(ids))
        .group_by(Conversation.assigned_agent_id))).all())


async def _members(session: AsyncSession, org: int, group_id: int | None, only_available: bool) -> list[int]:
    stmt = select(Agent.id).where(Agent.organization_id == org, Agent.is_active)
    if only_available:
        online = hub.online_agent_ids(org)
        if not online:
            return []
        stmt = stmt.where(Agent.availability == "available", Agent.id.in_(online))
    if group_id:
        stmt = stmt.join(AgentGroup, AgentGroup.agent_id == Agent.id).where(AgentGroup.group_id == group_id)
    return sorted(set((await session.scalars(stmt)).all()))


async def pick_agent(session: AsyncSession, org: int, group_id: int | None, contact_id: int | None = None,
                     exclude: set[int] | None = None, business_open: bool | None = None) -> int | None:
    group = await session.get(Group, group_id) if group_id else None
    if group is not None and group.organization_id != org:
        group = None
    routing = group.routing if group is not None else "least_loaded"
    if routing == "manual":
        return None
    cfg = await get_setting(session, "routing", org)
    exclude = exclude or set()
    candidates = [a for a in await _members(session, org, group_id, True) if a not in exclude]
    load = await _load(session, candidates)
    if group is not None and group.max_open_per_agent:
        candidates = [a for a in candidates if load.get(a, 0) < group.max_open_per_agent]

    if candidates and contact_id:
        contact = await session.get(Contact, contact_id)
        if contact is not None:
            if routing == "sticky_owner" and contact.owner_agent_id in candidates:
                return contact.owner_agent_id
            if cfg.get("sticky_agent"):  # "reasignar al mismo asesor si está disponible"
                for preferred in (contact.last_agent_id, contact.owner_agent_id):
                    if preferred in candidates:
                        return preferred

    if candidates:
        if routing == "round_robin" and group is not None:
            ordered = sorted(candidates)
            last = group.last_assigned_agent_id
            nxt = next((a for a in ordered if last is None or a > last), ordered[0])
            group.last_assigned_agent_id = nxt
            return nxt
        return min(candidates, key=lambda a: (load.get(a, 0), a))

    # Nadie disponible: en horario y con el ajuste activo, se asigna a un integrante no disponible
    if cfg.get("assign_when_none_available") and business_open is not False:
        if business_open is None:
            from app import business_hours

            business_open = (await business_hours.status(session, org, group_id))["open"]
        if business_open:
            members = [a for a in await _members(session, org, group_id, False) if a not in exclude]
            if members:
                mload = await _load(session, members)
                return min(members, key=lambda a: (mload.get(a, 0), a))
    return None


async def after_assignment(session: AsyncSession, conv: Conversation, agent_id: int | None,
                           by: str = "system") -> None:
    """Dueño del cliente en la primera asignación. El aviso al asesor lo genera app.notifications a partir del
    evento de asignación (con Web Push), así que aquí no se notifica para no duplicar."""
    del by
    if not agent_id:
        return
    contact = await session.get(Contact, conv.contact_id)
    if contact is not None and contact.owner_agent_id is None:
        cfg = await get_setting(session, "routing", conv.organization_id)
        if cfg.get("owner_on_first_assignment", True):
            contact.owner_agent_id, contact.owner_assigned_at = agent_id, utcnow()


def check_transfer(conv: Conversation, source_group: Group | None, target_group_id: int | None,
                   actor_role: str) -> None:
    """Un asesor solo transfiere a los grupos permitidos por el grupo actual (admin/supervisor sin límite)."""
    if actor_role in ("admin", "supervisor") or source_group is None or not target_group_id:
        return
    if target_group_id == source_group.id:
        return
    allowed = source_group.transfer_group_ids
    if allowed is not None and target_group_id not in allowed:
        raise HTTPException(422, f"El grupo «{source_group.name}» no permite transferir a ese grupo")


def serves_channel(group: Group | None, channel_id: int | None) -> bool:
    return group is None or not group.channel_ids or channel_id in (group.channel_ids or [])


async def groups_for_channel(session: AsyncSession, org: int, channel_id: int | None) -> list[Group]:
    rows = (await session.scalars(select(Group).where(Group.organization_id == org))).all()
    return [g for g in rows if serves_channel(g, channel_id)]


async def on_agent_deactivated(session: AsyncSession, agent: Agent, transfer_to: int | None = None) -> dict:
    """Reasigna las conversaciones abiertas del asesor dentro de sus grupos (o las deja en cola) y transfiere o
    libera los clientes de los que era dueño."""
    from app.db import set_actor

    org = agent.organization_id
    if transfer_to is not None:
        target = await session.get(Agent, transfer_to)
        if target is None or target.organization_id != org or not target.is_active or target.id == agent.id:
            raise HTTPException(422, "Asesor destino inválido")
    convs = (await session.scalars(select(Conversation).where(
        Conversation.organization_id == org, Conversation.assigned_agent_id == agent.id,
        Conversation.status == "human"))).unique().all()
    reassigned = queued = 0
    await set_actor(session, "system")
    for conv in convs:
        new = transfer_to or await pick_agent(session, org, conv.group_id, conv.contact_id, exclude={agent.id})
        conv.assigned_agent_id = new
        if new:
            reassigned += 1
            await after_assignment(session, conv, new, "transfer")
        else:
            queued += 1
    owned = (await session.execute(update(Contact).where(
        Contact.organization_id == org, Contact.owner_agent_id == agent.id).values(
        owner_agent_id=transfer_to, owner_assigned_at=utcnow() if transfer_to else None))).rowcount
    await session.commit()
    for conv in convs:
        await hub.broadcast("conversation.updated", {"id": conv.id, "assigned_agent_id": conv.assigned_agent_id}, org)
    return {"reassigned": reassigned, "queued": queued, "owners_moved": owned or 0}
