"""Roles y permisos granulares (docs/data-model.md §18.4).

Cada asesor tiene un rol (`agents.role_id` → `roles`). Los roles del sistema (admin / supervisor / asesor) se crean
por empresa con un trigger; sus permisos se completan desde este catálogo si están vacíos. El administrador del
sistema siempre tiene todos los permisos (no se puede editar). `agents.role` (admin | supervisor | agent) se
mantiene igual al `base_role` del rol para los chequeos heredados (`require_admin`).

Uso en un router:  `agent: Agent = Depends(require_permission("users.manage"))`
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.db import get_session
from app.models import Agent, Role

# Catálogo agrupado (clave → etiqueta). Las claves son estables: se guardan en roles.permissions.
CATALOG: dict[str, dict[str, str]] = {
    "Conversaciones": {
        "conversations.view_own": "Ver sus conversaciones",
        "conversations.view_group": "Ver las conversaciones de sus grupos",
        "conversations.view_all": "Ver todas las conversaciones",
        "conversations.reply": "Responder conversaciones",
        "conversations.assign": "Asignar y transferir conversaciones",
        "conversations.act_as_agent": "Actuar como otro asesor",
        "conversations.close": "Cerrar y tipificar",
    },
    "Clientes": {
        "contacts.view": "Ver clientes",
        "contacts.edit": "Editar clientes y campos",
        "contacts.block": "Bloquear clientes",
        "contacts.import": "Importar clientes",
        "contacts.merge": "Unir clientes duplicados",
        "deals.manage": "Gestionar negocios",
    },
    "Campañas": {
        "campaigns.view": "Ver campañas",
        "campaigns.send": "Crear y enviar campañas",
        "templates.manage": "Gestionar plantillas de WhatsApp",
    },
    "Flujos y automatizaciones": {
        "flows.view": "Ver flujos",
        "flows.manage": "Crear y publicar flujos",
        "automations.manage": "Gestionar automatizaciones y webhooks",
    },
    "IA": {
        "ai.manage": "Configurar agentes de IA y Cortex",
        "ai.knowledge": "Gestionar conocimiento, memoria y catálogo",
        "qa.review": "Revisar calidad (QA)",
    },
    "Reportes": {
        "reports.view_own": "Ver sus propios indicadores",
        "reports.view_group": "Ver reportes de sus grupos",
        "reports.view_all": "Ver todos los reportes",
        "reports.realtime": "Tablero en tiempo real",
    },
    "Configuración": {
        "settings.manage": "Cambiar configuraciones de la empresa",
        "channels.manage": "Conectar números y canales",
        "security.manage": "Seguridad: contraseñas, 2FA, IPs y SSO",
        "audit.view": "Ver la auditoría de acceso",
    },
    "Usuarios": {
        "users.view": "Ver usuarios",
        "users.manage": "Crear, editar y desactivar usuarios",
        "roles.manage": "Gestionar roles y permisos",
        "groups.manage": "Gestionar grupos",
    },
    "Integraciones": {
        "integrations.manage": "CRM, Ads, conversiones y atribución",
        "api_keys.manage": "Llaves de API y conectores",
    },
    "Facturación": {
        "billing.manage": "Plan y facturación",
    },
    "Datos sensibles": {
        "data.sensitive.view": "Ver documentos, fechas de nacimiento y datos sensibles sin enmascarar",
    },
    "Exportaciones": {
        "exports.contacts": "Exportar clientes",
        "exports.conversations": "Exportar conversaciones",
    },
}
ALL: frozenset[str] = frozenset(k for group in CATALOG.values() for k in group)

SUPERVISOR = frozenset({
    "conversations.view_own", "conversations.view_group", "conversations.reply", "conversations.assign",
    "conversations.act_as_agent", "conversations.close", "contacts.view", "contacts.edit", "contacts.block",
    "contacts.merge", "deals.manage", "campaigns.view", "flows.view", "qa.review", "reports.view_own",
    "reports.view_group", "reports.realtime", "users.view", "exports.contacts", "exports.conversations",
    "data.sensitive.view",
})
AGENT = frozenset({
    "conversations.view_own", "conversations.reply", "conversations.close", "contacts.view", "contacts.edit",
    "deals.manage", "reports.view_own",
})
SYSTEM_DEFAULTS: dict[str, frozenset[str]] = {"admin": ALL, "supervisor": SUPERVISOR, "agent": AGENT}
SCOPES = ("own", "groups", "all")
BASE_ROLES = ("admin", "supervisor", "agent")


def catalog_out() -> list[dict]:
    return [{"group": g, "permissions": [{"key": k, "label": v} for k, v in items.items()]}
            for g, items in CATALOG.items()]


async def role_of(session: AsyncSession, agent: Agent) -> Role | None:
    """Rol efectivo: agents.role_id o, si no tiene, el rol del sistema de su base (admin/supervisor/agent)."""
    if agent.role_id:
        role = await session.get(Role, agent.role_id)
        if role and role.organization_id == agent.organization_id:
            return role
    return (await session.scalars(select(Role).where(
        Role.organization_id == agent.organization_id, Role.key == agent.role, Role.is_system))).first()


def role_permissions(role: Role | None, fallback_base: str = "agent") -> frozenset[str]:
    if role is None:
        return SYSTEM_DEFAULTS.get(fallback_base, AGENT)
    if role.is_system and role.key == "admin":
        return ALL  # el administrador del sistema siempre tiene todo
    if role.is_system and not role.permissions:
        return SYSTEM_DEFAULTS.get(role.key, AGENT)
    return frozenset(p for p in role.permissions if p in ALL)


async def permissions_of(session: AsyncSession, agent: Agent) -> frozenset[str]:
    cached = getattr(agent, "_perm_cache", None)
    if cached is not None:
        return cached
    perms = role_permissions(await role_of(session, agent), agent.role)
    agent._perm_cache = perms  # vive lo que vive la petición
    return perms


async def has_permission(session: AsyncSession, agent: Agent, perm: str) -> bool:
    if perm not in ALL:
        raise ValueError(f"Permiso desconocido: {perm}")
    return perm in await permissions_of(session, agent)


def require_permission(perm: str):
    if perm not in ALL:
        raise ValueError(f"Permiso desconocido: {perm}")

    async def dependency(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)) -> Agent:
        if not await has_permission(session, agent, perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes permiso para esta acción")
        return agent

    dependency.__name__ = f"require_{perm.replace('.', '_')}"
    return dependency


async def ensure_system_role_permissions(session: AsyncSession, org: int) -> None:
    """Completa (idempotente) los permisos de los roles del sistema que estén vacíos."""
    rows = (await session.scalars(select(Role).where(Role.organization_id == org, Role.is_system))).all()
    for r in rows:
        if r.key == "admin":
            if set(r.permissions or []) != ALL:
                r.permissions = sorted(ALL)
        elif not r.permissions and r.key in SYSTEM_DEFAULTS:
            r.permissions = sorted(SYSTEM_DEFAULTS[r.key])
    await session.flush()
