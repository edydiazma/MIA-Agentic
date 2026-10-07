from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_token, current_agent, hash_password, require_admin, verify_password
from app.db import get_session
from app.models import Agent, AgentGroup, Conversation, Group, Organization, utcnow
from app.plans import enforce_limit
from app.realtime import hub
from app.schemas import AgentCreate, AgentOut, AgentUpdate, GroupOut, LoginIn, TokenOut

router = APIRouter(prefix="/api", tags=["users"])

ROLES = ("admin", "supervisor", "agent")
AVAILABILITY = ("available", "away", "busy")


class AgentDetail(AgentOut):
    group_ids: list[int]
    online: bool


class GroupIn(BaseModel):
    name: str
    description: str | None = None


async def _detail(session: AsyncSession, agents: list[Agent]) -> list[AgentDetail]:
    ids = [a.id for a in agents]
    rows = (await session.execute(
        select(AgentGroup.agent_id, AgentGroup.group_id).where(AgentGroup.agent_id.in_(ids)))).all() if ids else []
    groups: dict[int, list[int]] = {}
    for a, g in rows:
        groups.setdefault(a, []).append(g)
    online = hub.online_agent_ids()
    return [AgentDetail(**AgentOut.model_validate(a).model_dump(), group_ids=groups.get(a.id, []),
                        online=a.id in online) for a in agents]


async def _set_groups(session: AsyncSession, org: int, agent_id: int, group_ids: list[int]) -> None:
    valid = set((await session.scalars(
        select(Group.id).where(Group.organization_id == org, Group.id.in_(group_ids or [0])))).all())
    if set(group_ids) - valid:
        raise HTTPException(422, "Grupo inválido")
    await session.execute(delete(AgentGroup).where(AgentGroup.agent_id == agent_id))
    for gid in valid:
        session.add(AgentGroup(agent_id=agent_id, group_id=gid))


class LoginBody(LoginIn):
    organization_id: int | None = None  # requerido cuando el correo tiene acceso a varias empresas


class SwitchOrgIn(BaseModel):
    organization_id: int


async def _org_summary(session: AsyncSession, org_ids: list[int]) -> list[dict]:
    rows = (await session.execute(select(Organization.id, Organization.name, Organization.status)
                                  .where(Organization.id.in_(org_ids)).order_by(Organization.name))).all()
    return [{"id": i, "name": n, "status": st} for i, n, st in rows]


def _token_out(agent: Agent, orgs: list[int], organization: Organization | None) -> dict:
    return {**TokenOut(access_token=create_token(agent, orgs), agent=AgentOut.model_validate(agent)).model_dump(),
            "organization": {"id": organization.id, "name": organization.name, "status": organization.status}
            if organization else None}


@router.post("/auth/login")
async def login(body: LoginBody, session: AsyncSession = Depends(get_session)):
    """Si el correo pertenece a varias empresas (con esa misma contraseña), pide elegir una."""
    candidates = (await session.scalars(select(Agent).where(
        func.lower(Agent.email) == body.email.lower().strip(), Agent.is_active).order_by(Agent.organization_id))).all()
    matches = [a for a in candidates if verify_password(body.password, a.password_hash)]
    if not matches:
        raise HTTPException(401, "Credenciales inválidas")
    org_ids = [a.organization_id for a in matches]
    if body.organization_id is not None:
        chosen = next((a for a in matches if a.organization_id == body.organization_id), None)
        if not chosen:
            raise HTTPException(403, "No tienes acceso a esa empresa")
    elif len(matches) > 1:
        return {"choose_org": await _org_summary(session, org_ids)}
    else:
        chosen = matches[0]
    chosen.last_seen_at = utcnow()
    await session.commit()
    return _token_out(chosen, org_ids, await session.get(Organization, chosen.organization_id))


@router.get("/auth/orgs")
async def my_orgs(request: Request, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Empresas a las que puedes cambiar sin volver a iniciar sesión."""
    ids = [int(i) for i in getattr(request.state, "token_orgs", [agent.organization_id])]
    return {"current": agent.organization_id, "organizations": await _org_summary(session, ids)}


@router.post("/auth/switch-org")
async def switch_org(body: SwitchOrgIn, request: Request, agent: Agent = Depends(current_agent),
                     session: AsyncSession = Depends(get_session)):
    """Cambia de empresa: solo a las que este correo entró con su contraseña en el inicio de sesión."""
    allowed = [int(i) for i in getattr(request.state, "token_orgs", [agent.organization_id])]
    if body.organization_id not in allowed:
        raise HTTPException(403, "No tienes acceso a esa empresa (vuelve a iniciar sesión)")
    target = await session.scalar(select(Agent).where(
        Agent.organization_id == body.organization_id, func.lower(Agent.email) == agent.email.lower(), Agent.is_active))
    if not target:
        raise HTTPException(403, "No tienes acceso a esa empresa")
    return _token_out(target, allowed, await session.get(Organization, target.organization_id))


@router.get("/auth/me", response_model=AgentOut)
async def me(agent: Agent = Depends(current_agent)):
    return agent


@router.put("/auth/me/availability", response_model=AgentOut)
async def set_my_availability(
    body: dict, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)
):
    value = body.get("availability")
    if value not in AVAILABILITY:
        raise HTTPException(422, "Disponibilidad inválida")
    agent.availability = value
    await session.commit()
    await hub.broadcast("agent.presence", {"agent_id": agent.id, "availability": value}, agent.organization_id)
    return agent


@router.get("/agents", response_model=list[AgentDetail])
async def list_agents(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(
        select(Agent).where(Agent.organization_id == agent.organization_id).order_by(Agent.name))).all()
    return await _detail(session, list(rows))


@router.post("/agents", response_model=AgentDetail)
async def create_agent(
    body: AgentCreate, admin: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)
):
    email = body.email.lower().strip()
    if await session.scalar(select(Agent.id).where(Agent.organization_id == admin.organization_id,
                                                   func.lower(Agent.email) == email)):
        raise HTTPException(409, "Ya existe un usuario con ese correo")
    if body.role not in ROLES:
        raise HTTPException(422, "Rol inválido")
    if len(body.password) < 8:
        raise HTTPException(422, "La contraseña debe tener al menos 8 caracteres")
    await enforce_limit(session, admin.organization_id, "users")
    agent = Agent(organization_id=admin.organization_id, email=email, name=body.name,
                  password_hash=hash_password(body.password), role=body.role)
    session.add(agent)
    await session.flush()
    await _set_groups(session, admin.organization_id, agent.id, body.group_ids)
    await session.commit()
    return (await _detail(session, [agent]))[0]


@router.put("/agents/{agent_id}", response_model=AgentDetail)
async def update_agent(
    agent_id: int, body: AgentUpdate, admin: Agent = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if not agent or agent.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    data = body.model_dump(exclude_unset=True)
    if "role" in data and data["role"] not in ROLES:
        raise HTTPException(422, "Rol inválido")
    if "availability" in data and data["availability"] not in AVAILABILITY:
        raise HTTPException(422, "Disponibilidad inválida")
    if agent.id == admin.id and (data.get("is_active") is False or data.get("role") not in (None, "admin")):
        raise HTTPException(422, "No puedes desactivarte ni quitarte el rol de administrador")
    password = data.pop("password", None)
    if password:
        if len(password) < 8:
            raise HTTPException(422, "La contraseña debe tener al menos 8 caracteres")
        agent.password_hash = hash_password(password)
    if "group_ids" in data:
        await _set_groups(session, admin.organization_id, agent.id, data.pop("group_ids") or [])
    for k, v in data.items():
        setattr(agent, k, v)
    await session.commit()
    return (await _detail(session, [agent]))[0]


async def _group(session: AsyncSession, org: int, group_id: int) -> Group:
    g = await session.get(Group, group_id)
    if not g or g.organization_id != org:
        raise HTTPException(404, "Grupo no encontrado")
    return g


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return (await session.scalars(
        select(Group).where(Group.organization_id == agent.organization_id).order_by(Group.name))).all()


@router.post("/groups", response_model=GroupOut)
async def create_group(body: GroupIn, admin: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "El nombre es obligatorio")
    if await session.scalar(select(Group.id).where(Group.organization_id == admin.organization_id, Group.name == name)):
        raise HTTPException(409, "Ya existe un grupo con ese nombre")
    g = Group(organization_id=admin.organization_id, name=name, description=body.description)
    session.add(g)
    await session.commit()
    return g


@router.put("/groups/{group_id}", response_model=GroupOut)
async def update_group(group_id: int, body: GroupIn, admin: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    g = await _group(session, admin.organization_id, group_id)
    g.name, g.description = body.name.strip(), body.description
    await session.commit()
    return g


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int, admin: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    g = await _group(session, admin.organization_id, group_id)
    await session.execute(update(Conversation).where(Conversation.group_id == group_id).values(group_id=None))
    await session.delete(g)  # agent_groups cae en cascada
    await session.commit()
    return {"ok": True}
