"""Roles y permisos (§18.4): catálogo, roles propios, alcance de datos y asignación a usuarios."""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Agent, Role
from app.permissions import ALL, BASE_ROLES, SCOPES, catalog_out, ensure_system_role_permissions, require_permission
from app.security import audit

router = APIRouter(prefix="/api/roles", tags=["roles"])
MANAGE = Depends(require_permission("roles.manage"))


class RoleIn(BaseModel):
    name: str
    key: str | None = None
    description: str | None = None
    base_role: str = "agent"
    permissions: list[str] = []
    data_scope: str = "own"


def _out(r: Role, users: int = 0) -> dict:
    from app.permissions import role_permissions

    return {"id": r.id, "key": r.key, "name": r.name, "description": r.description, "base_role": r.base_role,
            "permissions": sorted(role_permissions(r, r.base_role)), "data_scope": r.data_scope,
            "is_system": r.is_system, "locked": r.is_system and r.key == "admin", "users": users,
            "created_at": r.created_at, "updated_at": r.updated_at}


def _validate(body: RoleIn, actor: Agent | None = None) -> None:
    if actor is not None and body.base_role == "admin" and actor.role != "admin":
        raise HTTPException(403, "Solo un administrador puede crear roles con base administrador")
    if body.base_role not in BASE_ROLES:
        raise HTTPException(422, "Rol base inválido")
    if body.data_scope not in SCOPES:
        raise HTTPException(422, "Alcance inválido (own | groups | all)")
    unknown = set(body.permissions) - ALL
    if unknown:
        raise HTTPException(422, f"Permisos desconocidos: {', '.join(sorted(unknown))}")
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")


async def _counts(session: AsyncSession, org: int) -> dict[int, int]:
    return dict((await session.execute(select(Agent.role_id, func.count()).where(
        Agent.organization_id == org, Agent.is_active).group_by(Agent.role_id))).all())


@router.get("/catalog")
async def catalog(_: Agent = Depends(require_permission("users.view"))):
    return {"groups": catalog_out(), "scopes": list(SCOPES), "base_roles": list(BASE_ROLES)}


@router.get("")
async def list_roles(agent: Agent = Depends(require_permission("users.view")),
                     session: AsyncSession = Depends(get_session)):
    await ensure_system_role_permissions(session, agent.organization_id)
    await session.commit()
    counts = await _counts(session, agent.organization_id)
    rows = (await session.scalars(select(Role).where(Role.organization_id == agent.organization_id)
                                  .order_by(Role.is_system.desc(), Role.name))).all()
    return [_out(r, counts.get(r.id, 0)) for r in rows]


def _key(raw: str) -> str:
    k = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")[:40]
    return k if k and k[0].isalpha() else f"rol_{k}"[:40]


@router.post("")
async def create_role(body: RoleIn, request: Request, agent: Agent = MANAGE,
                      session: AsyncSession = Depends(get_session)):
    _validate(body, agent)
    key = _key(body.key or body.name)
    if await session.scalar(select(Role.id).where(Role.organization_id == agent.organization_id, Role.key == key)):
        raise HTTPException(409, "Ya existe un rol con ese nombre")
    r = Role(organization_id=agent.organization_id, key=key, name=body.name.strip(), description=body.description,
             base_role=body.base_role, permissions=sorted(set(body.permissions)), data_scope=body.data_scope)
    session.add(r)
    await session.commit()
    await audit.record("role_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, change="role_created", role=key)
    return _out(r)


async def _role(session: AsyncSession, org: int, role_id: int) -> Role:
    r = await session.get(Role, role_id)
    if not r or r.organization_id != org:
        raise HTTPException(404, "Rol no encontrado")
    return r


@router.put("/{role_id}")
async def update_role(role_id: int, body: RoleIn, request: Request, agent: Agent = MANAGE,
                      session: AsyncSession = Depends(get_session)):
    r = await _role(session, agent.organization_id, role_id)
    if r.is_system and r.key == "admin":
        raise HTTPException(403, "El rol Administrador tiene todos los permisos y no se puede editar")
    _validate(body, agent)
    if agent.role_id == r.id and agent.role != "admin":
        raise HTTPException(403, "No puedes editar tu propio rol")
    if r.is_system and body.base_role != r.base_role:
        raise HTTPException(422, "No se puede cambiar el rol base de un rol del sistema")
    r.name, r.description, r.data_scope = body.name.strip(), body.description, body.data_scope
    r.permissions = sorted(set(body.permissions))
    if r.base_role != body.base_role:
        r.base_role = body.base_role
        await session.execute(update(Agent).where(Agent.role_id == r.id).values(role=body.base_role))
    await session.commit()
    await audit.record("role_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, change="role_updated", role=r.key)
    return _out(r, (await _counts(session, agent.organization_id)).get(r.id, 0))


@router.post("/{role_id}/duplicate")
async def duplicate_role(role_id: int, request: Request, agent: Agent = MANAGE,
                         session: AsyncSession = Depends(get_session)):
    src = await _role(session, agent.organization_id, role_id)
    body = RoleIn(name=f"{src.name} (copia)", description=src.description,
                  base_role=src.base_role, permissions=_out(src)["permissions"], data_scope=src.data_scope)
    n = 1
    while await session.scalar(select(Role.id).where(Role.organization_id == agent.organization_id,
                                                     Role.key == _key(body.name))):
        n += 1
        body.name = f"{src.name} (copia {n})"
    return await create_role(body, request, agent, session)


@router.delete("/{role_id}")
async def delete_role(role_id: int, request: Request, agent: Agent = MANAGE,
                      session: AsyncSession = Depends(get_session)):
    """Los usuarios del rol pasan al rol del sistema de su mismo rol base."""
    r = await _role(session, agent.organization_id, role_id)
    if r.is_system:
        raise HTTPException(403, "Los roles del sistema no se pueden eliminar")
    fallback = await session.scalar(select(Role).where(Role.organization_id == agent.organization_id,
                                                       Role.key == r.base_role, Role.is_system))
    await session.execute(update(Agent).where(Agent.role_id == r.id)
                          .values(role_id=fallback.id if fallback else None, role=r.base_role))
    await session.delete(r)
    await session.commit()
    await audit.record("role_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, change="role_deleted", role=r.key)
    return {"ok": True}


class AssignIn(BaseModel):
    role_id: int


@router.put("/assign/{agent_id}")
async def assign_role(agent_id: int, body: AssignIn, request: Request,
                      admin: Agent = Depends(require_permission("users.manage")),
                      session: AsyncSession = Depends(get_session)):
    target = await session.get(Agent, agent_id)
    if not target or target.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    r = await _role(session, admin.organization_id, body.role_id)
    if target.id == admin.id and r.base_role != "admin":
        raise HTTPException(422, "No puedes quitarte el rol de administrador")
    if r.base_role == "admin" and admin.role != "admin":
        raise HTTPException(403, "Solo un administrador puede asignar un rol administrador")
    target.role_id, target.role = r.id, r.base_role
    await session.commit()
    await audit.record("role_changed", request, organization_id=admin.organization_id, agent_id=target.id,
                       email=target.email, by=admin.id, role=r.key)
    return {"ok": True, "agent_id": target.id, "role": _out(r)}
