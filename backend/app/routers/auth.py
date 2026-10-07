from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_token, current_agent, hash_password, require_admin, verify_password
from app.db import get_session
from app.models import Agent, AgentGroup, Conversation, Group, Organization, Role, utcnow
from app.ops import ratelimit
from app.permissions import permissions_of, require_permission, role_of
from app.security import audit, mfa, sso
from app.security import policy as sec_policy
from app.plans import enforce_limit
from app.realtime import hub
from app.scope import agent_clause, scope_for
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
    # Conserva el rol en los grupos que se mantienen (supervisor / miembro, §18.2)
    roles = dict((await session.execute(select(AgentGroup.group_id, AgentGroup.role)
                                        .where(AgentGroup.agent_id == agent_id))).all())
    await session.execute(delete(AgentGroup).where(AgentGroup.agent_id == agent_id))
    for gid in valid:
        session.add(AgentGroup(agent_id=agent_id, group_id=gid, role=roles.get(gid, "member")))


class LoginBody(LoginIn):
    organization_id: int | None = None  # requerido cuando el correo tiene acceso a varias empresas
    device_token: str | None = None  # dispositivo de confianza (no vuelve a pedir el segundo factor)


class SwitchOrgIn(BaseModel):
    organization_id: int


async def _org_summary(session: AsyncSession, org_ids: list[int]) -> list[dict]:
    rows = (await session.execute(select(Organization.id, Organization.name, Organization.status)
                                  .where(Organization.id.in_(org_ids)).order_by(Organization.name))).all()
    return [{"id": i, "name": n, "status": st} for i, n, st in rows]


def _token_out(agent: Agent, orgs: list[int], organization: Organization | None, minutes: int | None = None) -> dict:
    return {**TokenOut(access_token=create_token(agent, orgs, minutes), agent=AgentOut.model_validate(agent)).model_dump(),
            "organization": {"id": organization.id, "name": organization.name, "status": organization.status}
            if organization else None,
            "must_change_password": bool(agent.must_change_password)}


GENERIC_FAIL = "Correo o contraseña incorrectos"
IP_FAIL_LIMIT = 30  # intentos fallidos por IP en 15 min


async def finish_login(session: AsyncSession, agent: Agent, orgs: list[int], request: Request,
                       policy: dict | None = None, method: str = "password") -> dict:
    """Último paso común (contraseña, 2FA o SSO): token según la política, ganchos y auditoría."""
    policy = policy or await sec_policy.get_policy(session, agent.organization_id)
    agent.last_seen_at = utcnow()
    agent.failed_logins, agent.locked_until = 0, None
    await session.commit()
    try:  # estado del asesor al ingresar (pista de operación)
        from app import statuses

        await statuses.on_login(session, agent, request)
    except (ImportError, AttributeError):
        pass
    await audit.record("login_ok" if method != "sso" else "sso_login", request, organization_id=agent.organization_id,
                       agent_id=agent.id, email=agent.email, method=method)
    return _token_out(agent, orgs, await session.get(Organization, agent.organization_id),
                      int(policy.get("session_timeout_minutes") or 0) or None)


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, session: AsyncSession = Depends(get_session)):
    """Contraseña → (empresa) → IP permitida → bloqueo/vencimiento → segundo factor → token.

    Si el correo pertenece a varias empresas (con esa misma contraseña), pide elegir una."""
    email = body.email.lower().strip()
    ip = audit.client_ip(request)
    if await ratelimit.peek(f"login_fail:ip:{ip}", 900) >= IP_FAIL_LIMIT:
        await audit.record("login_failed", request, email=email, reason="rate_limited")
        raise HTTPException(429, "Demasiados intentos fallidos. Espera unos minutos e intenta de nuevo.")
    enforced = await sso.enforced_for(session, email)
    if enforced:
        await audit.record("login_failed", request, organization_id=enforced.organization_id, email=email,
                           reason="sso_enforced")
        raise HTTPException(403, {"message": "Tu empresa usa inicio de sesión único (SSO).",
                                  "sso": {"slug": enforced.slug, "protocol": enforced.protocol}})
    candidates = (await session.scalars(select(Agent).where(
        func.lower(Agent.email) == email, Agent.is_active).order_by(Agent.organization_id))).all()
    now = utcnow()
    unlocked = [a for a in candidates if not (a.locked_until and a.locked_until > now)]
    if candidates and not unlocked:
        await audit.record("login_failed", request, email=email, reason="locked")
        raise HTTPException(423, "Cuenta bloqueada temporalmente por intentos fallidos. "
                                 "Intenta más tarde o recupera tu contraseña.")
    matches = [a for a in unlocked if verify_password(body.password, a.password_hash)]
    if not matches:
        await ratelimit.hits(session, f"login_fail:ip:{ip}", 900)
        for a in unlocked:
            pol = await sec_policy.get_policy(session, a.organization_id)
            a.failed_logins = (a.failed_logins or 0) + 1
            if a.failed_logins >= int(pol.get("lockout_attempts", 5)):
                a.locked_until = now + timedelta(minutes=int(pol.get("lockout_minutes", 15)))
                await audit.record("locked", request, organization_id=a.organization_id, agent_id=a.id, email=email)
        await session.commit()
        await audit.record("login_failed", request, email=email, reason="bad_credentials")
        raise HTTPException(401, GENERIC_FAIL)
    org_ids = [a.organization_id for a in matches]
    if body.organization_id is not None:
        chosen = next((a for a in matches if a.organization_id == body.organization_id), None)
        if not chosen:
            raise HTTPException(403, "No tienes acceso a esa empresa")
    elif len(matches) > 1:
        return {"choose_org": await _org_summary(session, org_ids)}
    else:
        chosen = matches[0]

    pol = await sec_policy.get_policy(session, chosen.organization_id)
    if not sec_policy.ip_allowed(ip, pol.get("allowed_ips") or []):
        await audit.record("ip_blocked", request, organization_id=chosen.organization_id, agent_id=chosen.id,
                           email=email)
        raise HTTPException(403, "Tu empresa no permite ingresar desde esta red")
    if sec_policy.password_expired(chosen, pol):
        chosen.must_change_password = True

    needs_mfa = bool(chosen.mfa_method) or sec_policy.mfa_required(chosen, pol)
    if needs_mfa and not await mfa.trusted(session, chosen, body.device_token, ip):
        token, methods, code = await mfa.start_challenge(session, chosen, ip)
        sent = True
        if code:
            from app.onboarding.invites import send_email

            sent = await send_email(chosen.email, "Tu código de acceso",
                                    f"Tu código para ingresar es {code}. Vence en 10 minutos.\n"
                                    "Si no fuiste tú, cambia tu contraseña.")
        if code and not sent and not chosen.mfa_method:
            # 2FA exigido pero sin correo configurado ni app autenticadora: entra y debe activar 2FA ya
            await session.commit()
            out = await finish_login(session, chosen, org_ids, request, pol)
            return {**out, "mfa_enrollment_required": True}
        await session.commit()
        await audit.record("mfa_challenge", request, organization_id=chosen.organization_id, agent_id=chosen.id,
                           email=email, method=methods[0])
        return {"mfa_required": True, "mfa_token": token, "methods": methods,
                "email_hint": _mask_email(chosen.email) if "email" in methods else None}
    out = await finish_login(session, chosen, org_ids, request, pol)
    if sec_policy.mfa_required(chosen, pol) and not chosen.mfa_method:
        out["mfa_enrollment_required"] = True
    return out


def _mask_email(email: str) -> str:
    user, _, domain = email.partition("@")
    return (user[:2] + "***@" + domain) if domain else "***"


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


@router.get("/auth/me")
async def me(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Sesión: rol efectivo, permisos, estado del segundo factor y si debe cambiar la contraseña."""
    role = await role_of(session, agent)
    pol = await sec_policy.get_policy(session, agent.organization_id)
    return {**AgentOut.model_validate(agent).model_dump(),
            "role_info": {"id": role.id, "key": role.key, "name": role.name, "data_scope": role.data_scope}
            if role else {"id": None, "key": agent.role, "name": agent.role, "data_scope": "own"},
            "permissions": sorted(await permissions_of(session, agent)),
            "mfa": {"enabled": bool(agent.mfa_method), "method": agent.mfa_method,
                    "required": sec_policy.mfa_required(agent, pol),
                    "recovery_codes_left": len(agent.mfa_recovery_hashes or [])},
            "must_change_password": bool(agent.must_change_password) or sec_policy.password_expired(agent, pol),
            "has_password": bool(agent.password_hash)}


@router.post("/auth/logout")
async def logout(request: Request, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    try:
        from app import statuses

        await statuses.on_logout(session, agent, request)
    except (ImportError, AttributeError):
        pass
    await audit.record("logout", request, organization_id=agent.organization_id, agent_id=agent.id, email=agent.email)
    return {"ok": True}


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
    stmt = select(Agent).where(Agent.organization_id == agent.organization_id).order_by(Agent.name)
    clause = agent_clause(await scope_for(session, agent))  # supervisor: asesores de sus grupos (app/scope.py)
    if clause is not None:
        stmt = stmt.where(clause)
    rows = (await session.scalars(stmt)).all()
    return await _detail(session, list(rows))


@router.post("/agents", response_model=AgentDetail)
async def create_agent(
    body: AgentCreate, request: Request, admin: Agent = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
):
    email = body.email.lower().strip()
    if await session.scalar(select(Agent.id).where(Agent.organization_id == admin.organization_id,
                                                   func.lower(Agent.email) == email)):
        raise HTTPException(409, "Ya existe un usuario con ese correo")
    role_id, base = await _resolve_role(session, admin, body.role, getattr(body, "role_id", None))
    pol = await sec_policy.get_policy(session, admin.organization_id)
    errors = sec_policy.strength_errors(pol, body.password, email, body.name)
    if errors:
        raise HTTPException(422, "Contraseña: " + "; ".join(errors))
    await enforce_limit(session, admin.organization_id, "users")
    agent = Agent(organization_id=admin.organization_id, email=email, name=body.name,
                  password_hash=hash_password(body.password), role=base, role_id=role_id,
                  password_changed_at=utcnow(), must_change_password=bool(getattr(body, "must_change_password", False)))
    session.add(agent)
    await session.flush()
    await _set_groups(session, admin.organization_id, agent.id, body.group_ids)
    await session.commit()
    return (await _detail(session, [agent]))[0]


@router.put("/agents/{agent_id}", response_model=AgentDetail)
async def update_agent(
    agent_id: int, body: AgentUpdate, request: Request, admin: Agent = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if not agent or agent.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    data = body.model_dump(exclude_unset=True)
    if "availability" in data and data["availability"] not in AVAILABILITY:
        raise HTTPException(422, "Disponibilidad inválida")
    if "role" in data or "role_id" in data:
        role_id, base = await _resolve_role(session, admin, data.pop("role", None), data.pop("role_id", None))
        if agent.id == admin.id and base != "admin":
            raise HTTPException(422, "No puedes desactivarte ni quitarte el rol de administrador")
        if (role_id, base) != (agent.role_id, agent.role):
            await audit.record("role_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                               email=agent.email, by=admin.id, role_id=role_id)
        agent.role_id, agent.role = role_id, base
    if agent.id == admin.id and data.get("is_active") is False:
        raise HTTPException(422, "No puedes desactivarte ni quitarte el rol de administrador")
    password = data.pop("password", None)
    if password:
        pol = await sec_policy.get_policy(session, agent.organization_id)
        errors = await sec_policy.validate_new_password(session, agent, password, pol)
        if errors:
            raise HTTPException(422, "Contraseña: " + "; ".join(errors))
        await sec_policy.set_password(session, agent, password, must_change=True)
        await audit.record("password_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                           email=agent.email, by=admin.id)
    if "group_ids" in data:
        await _set_groups(session, admin.organization_id, agent.id, data.pop("group_ids") or [])
    deactivating = agent.is_active and data.get("is_active") is False
    for k, v in data.items():
        setattr(agent, k, v)
    await session.commit()
    if deactivating:  # reasigna sus conversaciones y libera sus clientes (pista de operación, §18.1)
        try:
            from app import routing, statuses

            await routing.on_agent_deactivated(session, agent)
            await statuses.on_logout(session, agent, reason="deactivated")
        except (ImportError, AttributeError):
            pass
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


async def _resolve_role(session: AsyncSession, admin: Agent, role_key: str | None, role_id: int | None) -> tuple[int | None, str]:
    """(role_id, base_role) a partir de un role_id (rol propio) o una clave heredada (admin | supervisor | agent)."""
    if role_id is not None:
        role = await session.get(Role, role_id)
        if not role or role.organization_id != admin.organization_id:
            raise HTTPException(422, "Rol inválido")
        if role.base_role == "admin" and admin.role != "admin":
            raise HTTPException(403, "Solo un administrador puede asignar un rol administrador")
        return role.id, role.base_role
    key = role_key or "agent"
    if key not in ROLES:
        raise HTTPException(422, "Rol inválido")
    if key == "admin" and admin.role != "admin":
        raise HTTPException(403, "Solo un administrador puede asignar un rol administrador")
    role = await session.scalar(select(Role).where(Role.organization_id == admin.organization_id, Role.key == key,
                                                   Role.is_system))
    return (role.id if role else None), key
