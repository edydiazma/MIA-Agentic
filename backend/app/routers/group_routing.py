"""Reglas de enrutamiento por grupo, dueño del cliente y desactivación de asesores (§18.1)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import routing
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, AgentGroup, Channel, Contact, Group, utcnow

router = APIRouter(prefix="/api", tags=["routing"])
ROUTING = ("least_loaded", "round_robin", "sticky_owner", "manual")


class GroupSettingsIn(BaseModel):
    routing: str = "least_loaded"
    transfer_group_ids: list[int] | None = None   # null = a cualquier grupo; [] = a ninguno
    channel_ids: list[int] = []                    # vacío = todos los canales
    max_open_per_agent: int | None = Field(default=None, ge=1, le=500)
    supervisor_ids: list[int] | None = None        # integrantes con rol supervisor en el grupo


class OwnerIn(BaseModel):
    agent_id: int | None = None


class BulkOwnerIn(OwnerIn):
    contact_ids: list[int] = Field(min_length=1, max_length=5000)


class DeactivateIn(BaseModel):
    transfer_to: int | None = None   # asesor que recibe conversaciones abiertas y clientes; null = enrutar / cola


async def _group(session: AsyncSession, org: int, gid: int) -> Group:
    g = await session.get(Group, gid)
    if not g or g.organization_id != org:
        raise HTTPException(404, "Grupo no encontrado")
    return g


async def _settings_out(session: AsyncSession, g: Group) -> dict:
    members = (await session.execute(select(AgentGroup.agent_id, AgentGroup.role).where(
        AgentGroup.group_id == g.id))).all()
    return {"group_id": g.id, "name": g.name, "routing": g.routing, "transfer_group_ids": g.transfer_group_ids,
            "channel_ids": list(g.channel_ids or []), "max_open_per_agent": g.max_open_per_agent,
            "members": [a for a, _r in members], "supervisor_ids": [a for a, r in members if r == "supervisor"]}


@router.get("/groups/settings")
async def list_group_settings(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    groups = (await session.scalars(select(Group).where(Group.organization_id == agent.organization_id)
                                    .order_by(Group.name))).all()
    return [await _settings_out(session, g) for g in groups]


@router.get("/groups/{gid}/settings")
async def get_group_settings(gid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await _settings_out(session, await _group(session, agent.organization_id, gid))


@router.put("/groups/{gid}/settings")
async def put_group_settings(gid: int, body: GroupSettingsIn, admin: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    g = await _group(session, org, gid)
    if body.routing not in ROUTING:
        raise HTTPException(422, f"Estrategia inválida: {', '.join(ROUTING)}")
    if body.transfer_group_ids:
        valid = set((await session.scalars(select(Group.id).where(
            Group.organization_id == org, Group.id.in_(body.transfer_group_ids)))).all())
        if set(body.transfer_group_ids) - valid:
            raise HTTPException(422, "Grupo destino inválido")
    if body.channel_ids:
        valid = set((await session.scalars(select(Channel.id).where(
            Channel.organization_id == org, Channel.id.in_(body.channel_ids)))).all())
        if set(body.channel_ids) - valid:
            raise HTTPException(422, "Canal inválido")
    g.routing, g.transfer_group_ids = body.routing, body.transfer_group_ids
    g.channel_ids, g.max_open_per_agent = body.channel_ids, body.max_open_per_agent
    if body.supervisor_ids is not None:
        members = {a for a in (await session.scalars(select(AgentGroup.agent_id).where(AgentGroup.group_id == g.id))).all()}
        if set(body.supervisor_ids) - members:
            raise HTTPException(422, "Los supervisores deben ser integrantes del grupo")
        await session.execute(update(AgentGroup).where(AgentGroup.group_id == g.id).values(role="member"))
        if body.supervisor_ids:
            await session.execute(update(AgentGroup).where(
                AgentGroup.group_id == g.id, AgentGroup.agent_id.in_(body.supervisor_ids)).values(role="supervisor"))
    await session.commit()
    return await _settings_out(session, g)


async def _target(session: AsyncSession, org: int, agent_id: int | None) -> int | None:
    if agent_id is None:
        return None
    a = await session.get(Agent, agent_id)
    if not a or a.organization_id != org or not a.is_active:
        raise HTTPException(422, "Asesor inválido")
    return a.id


@router.put("/contacts/{contact_id}/owner")
async def set_owner(contact_id: int, body: OwnerIn, me: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    """Asignar el cliente a un asesor (o a mí); null lo libera. Un asesor solo puede tomarlo o soltarlo."""
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != me.organization_id:
        raise HTTPException(404, "Cliente no encontrado")
    target = await _target(session, me.organization_id, body.agent_id)
    if me.role == "agent" and target not in (None, me.id):
        raise HTTPException(403, "Solo puedes asignarte el cliente a ti")
    if me.role == "agent" and target is None and c.owner_agent_id not in (None, me.id):
        raise HTTPException(403, "El cliente es de otro asesor")
    c.owner_agent_id, c.owner_assigned_at = target, utcnow() if target else None
    await session.commit()
    return {"contact_id": c.id, "owner_agent_id": c.owner_agent_id, "owner_assigned_at": c.owner_assigned_at}


@router.post("/contacts/owner/bulk")
async def bulk_owner(body: BulkOwnerIn, admin: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if admin.role not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo supervisores y administradores")
    target = await _target(session, admin.organization_id, body.agent_id)
    res = await session.execute(update(Contact).where(
        Contact.organization_id == admin.organization_id, Contact.id.in_(body.contact_ids)).values(
        owner_agent_id=target, owner_assigned_at=utcnow() if target else None))
    await session.commit()
    return {"updated": res.rowcount or 0, "owner_agent_id": target}


@router.post("/agents/{agent_id}/deactivate")
async def deactivate(agent_id: int, body: DeactivateIn, admin: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    """Desactiva al asesor: reasigna sus conversaciones abiertas y mueve (o libera) sus clientes."""
    a = await session.get(Agent, agent_id)
    if not a or a.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    if a.id == admin.id:
        raise HTTPException(422, "No puedes desactivarte")
    a.is_active = False
    await session.flush()
    result = await routing.on_agent_deactivated(session, a, body.transfer_to)
    from app import statuses

    try:
        await statuses.on_logout(session, a, reason="deactivated")
    except Exception:  # la bitácora de estado no impide desactivar
        await session.rollback()
    return {"agent_id": a.id, "is_active": False, **result}
