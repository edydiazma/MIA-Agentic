"""Alcance de datos por rol (docs/data-model.md §18.2).

- admin → toda la empresa.
- supervisor → los grupos que supervisa (agent_groups.role = 'supervisor'): conversaciones de esos grupos (incluida
  su cola sin asignar), las asignadas a asesores de esos grupos y las propias. Un supervisor que aún no supervisa
  ningún grupo conserva la vista completa (compatibilidad con instalaciones previas a la migración 27).
- asesor del sistema → sin cambios (ve la bandeja de la empresa, como antes).
- roles propios (roles.is_system = false) → su `data_scope`: all | groups | own (own = asignadas a él + la cola sin
  asignar de sus grupos).

Las consultas reciben un `Scope` y aplican `conversation_clause` / `contact_clause` / `agent_clause`; el detalle
fuera de alcance responde 404 (no se revela que existe).
"""

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import exists, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentGroup, Contact, Conversation


@dataclass
class Scope:
    kind: str  # all | groups | own
    agent_id: int
    group_ids: set[int] = field(default_factory=set)  # groups: supervisados; own: grupos donde es miembro
    member_ids: set[int] = field(default_factory=set)  # groups: asesores de esos grupos

    @property
    def unrestricted(self) -> bool:
        return self.kind == "all"


async def _role_scope(session: AsyncSession, agent: Agent) -> str | None:
    """data_scope de un rol propio (si la migración 29 / app.permissions lo definen)."""
    role_id = getattr(agent, "role_id", None)
    if not role_id:
        return None
    try:
        from app.models import Role
    except ImportError:  # pragma: no cover
        return None
    role = await session.get(Role, role_id)
    if role is None or role.organization_id != agent.organization_id or role.is_system:
        return None
    return role.data_scope


async def scope_for(session: AsyncSession, agent: Agent) -> Scope:
    kind = await _role_scope(session, agent)
    if kind is None:
        kind = {"admin": "all", "supervisor": "groups"}.get(agent.role, "all")  # asesor del sistema: sin cambios
    if kind == "all":
        return Scope("all", agent.id)
    if kind == "groups":
        groups = set((await session.scalars(select(AgentGroup.group_id).where(
            AgentGroup.agent_id == agent.id, AgentGroup.role == "supervisor"))).all())
        if not groups and agent.role == "supervisor" and await _role_scope(session, agent) is None:
            return Scope("all", agent.id)  # supervisor sin grupos supervisados: vista completa (legado)
        members = set((await session.scalars(select(AgentGroup.agent_id).where(
            AgentGroup.group_id.in_(sorted(groups) or [0])))).all())
        return Scope("groups", agent.id, groups, members)
    groups = set((await session.scalars(select(AgentGroup.group_id).where(AgentGroup.agent_id == agent.id))).all())
    return Scope("own", agent.id, groups)


async def visible_group_ids(session: AsyncSession, agent: Agent) -> set[int] | None:
    """Grupos visibles (None = todos)."""
    s = await scope_for(session, agent)
    return None if s.unrestricted else set(s.group_ids)


def conversation_clause(s: Scope, model=Conversation):
    """Filtro SQL de conversaciones visibles (None = sin filtro)."""
    if s.unrestricted:
        return None
    if s.kind == "groups":
        return or_(model.group_id.in_(sorted(s.group_ids) or [0]), model.assigned_agent_id.in_(sorted(s.member_ids) or [0]),
                   model.assigned_agent_id == s.agent_id)
    return or_(model.assigned_agent_id == s.agent_id,
               (model.assigned_agent_id.is_(None)) & (model.group_id.in_(sorted(s.group_ids) or [0])))


def contact_clause(s: Scope):
    """Clientes visibles: con alguna conversación en alcance o cuyo dueño está en alcance."""
    if s.unrestricted:
        return None
    conv = conversation_clause(s)
    owners = {s.agent_id} | (s.member_ids if s.kind == "groups" else set())
    clauses = [exists().where(Conversation.contact_id == Contact.id, conv)]
    if hasattr(Contact, "owner_agent_id"):
        clauses.append(Contact.owner_agent_id.in_(sorted(owners)))
    return or_(*clauses)


def agent_clause(s: Scope):
    """Asesores visibles en listas y reportes."""
    if s.unrestricted:
        return None
    if s.kind == "groups":
        return Agent.id.in_(sorted(s.member_ids | {s.agent_id}))
    return Agent.id == s.agent_id


def group_allowed(s: Scope, group_id: int | None) -> bool:
    return s.unrestricted or (group_id is not None and group_id in s.group_ids)


def agent_allowed(s: Scope, agent_id: int | None) -> bool:
    if s.unrestricted:
        return True
    if agent_id is None:
        return False
    return agent_id == s.agent_id or (s.kind == "groups" and agent_id in s.member_ids)


def conversation_visible(s: Scope, conv: Conversation) -> bool:
    if s.unrestricted:
        return True
    if s.kind == "groups":
        return (conv.group_id in s.group_ids) or (conv.assigned_agent_id in s.member_ids) \
            or conv.assigned_agent_id == s.agent_id
    return conv.assigned_agent_id == s.agent_id or (conv.assigned_agent_id is None and conv.group_id in s.group_ids)


async def require_conversation(session: AsyncSession, agent: Agent, conv: Conversation | None) -> Conversation:
    """404 si no existe, es de otra empresa o está fuera del alcance del usuario."""
    if conv is None or conv.organization_id != agent.organization_id:
        raise HTTPException(404, "Conversación no encontrada")
    if not conversation_visible(await scope_for(session, agent), conv):
        raise HTTPException(404, "Conversación no encontrada")
    return conv


def apply(stmt, clause):
    return stmt if clause is None else stmt.where(clause)


def nothing():
    return false()
