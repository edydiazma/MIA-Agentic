"""Seguridad de acceso (§18.4): segundo factor, cambio y recuperación de contraseña, política de la empresa,
auditoría, desbloqueo y conexiones SSO (administración). Los pasos públicos de SSO están en routers/sso_public.py."""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, verify_password
from app.config import get_settings
from app.db import get_session
from app.models import (
    Agent,
    AuthEvent,
    MfaChallenge,
    Organization,
    PasswordReset,
    Role,
    SSOConnection,
    TrustedDevice,
    utcnow,
)
from app.ops import ratelimit
from app.permissions import require_permission
from app.routers.auth import finish_login
from app.secrets_vault import delete_secret, get_secret, put_secret
from app.security import audit, crypto, mfa, sso, totp
from app.security import policy as sec_policy
from app.settings_store import set_setting

router = APIRouter(prefix="/api", tags=["security"])
RESET_TTL = timedelta(minutes=30)


# --- Segundo factor en el inicio de sesión -------------------------------------------------------------------
class MfaVerifyIn(BaseModel):
    mfa_token: str
    code: str
    remember_device: bool = False
    device_token: str | None = None


@router.post("/auth/mfa/verify")
async def mfa_verify(body: MfaVerifyIn, request: Request, session: AsyncSession = Depends(get_session)):
    ch = await mfa.load_challenge(session, body.mfa_token)
    if not ch:
        raise HTTPException(401, "El código venció o se superaron los intentos: inicia sesión de nuevo")
    agent = await session.get(Agent, ch.agent_id)
    if not agent or not agent.is_active:
        raise HTTPException(401, "El código venció o se superaron los intentos: inicia sesión de nuevo")
    used = await mfa.check_code(session, agent, ch, body.code)
    if not used:
        await session.commit()
        await audit.record("mfa_failed", request, organization_id=agent.organization_id, agent_id=agent.id,
                           email=agent.email)
        raise HTTPException(401, "Código incorrecto")
    ch.verified_at = utcnow()
    pol = await sec_policy.get_policy(session, agent.organization_id)
    device = None
    if body.remember_device:
        device = await mfa.remember(session, agent, audit.client_ip(request), audit.user_agent(request),
                                    int(pol.get("trusted_device_days") or 0), body.device_token)
    await audit.record("mfa_ok", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, method=used)
    out = await finish_login(session, agent, [agent.organization_id], request, pol, method=f"mfa_{used}")
    return {**out, "device_token": device, "recovery_codes_left": len(agent.mfa_recovery_hashes or [])}


class MfaTokenIn(BaseModel):
    mfa_token: str


@router.post("/auth/mfa/resend")
async def mfa_resend(body: MfaTokenIn, request: Request, session: AsyncSession = Depends(get_session)):
    """Nuevo código por correo para el mismo reto (máx. 3 por 10 minutos)."""
    ch = await mfa.load_challenge(session, body.mfa_token)
    if not ch or ch.method != "email":
        raise HTTPException(401, "Inicia sesión de nuevo")
    if not await ratelimit.allow(session, f"mfa_resend:{ch.id}", 3, 600):
        raise HTTPException(429, "Ya enviamos varios códigos: espera unos minutos")
    agent = await session.get(Agent, ch.agent_id)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    ch.code_hash = crypto.sha256(f"{agent.id}:{code}")
    await session.commit()
    from app.onboarding.invites import send_email

    await send_email(agent.email, "Tu código de acceso", f"Tu código para ingresar es {code}. Vence en 10 minutos.")
    return {"ok": True}


# --- Mi seguridad: contraseña, 2FA, dispositivos ----------------------------------------------------------------
class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/password")
async def change_password(body: PasswordChangeIn, request: Request, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    if agent.password_hash and not verify_password(body.current_password, agent.password_hash):
        await audit.record("login_failed", request, organization_id=agent.organization_id, agent_id=agent.id,
                           email=agent.email, reason="bad_current_password")
        raise HTTPException(422, "La contraseña actual no es correcta")
    errors = await sec_policy.validate_new_password(session, agent, body.new_password)
    if errors:
        raise HTTPException(422, {"message": "La contraseña no cumple la política", "errors": errors})
    await sec_policy.set_password(session, agent, body.new_password)
    await session.commit()
    await audit.record("password_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email)
    return {"ok": True}


@router.get("/auth/password/policy")
async def my_password_policy(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    pol = await sec_policy.get_policy(session, agent.organization_id)
    return {k: pol[k] for k in ("min_length", "require_upper", "require_lower", "require_digit", "require_symbol",
                                "history", "expiry_days")}


@router.post("/auth/mfa/totp/setup")
async def totp_setup(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Semilla nueva (aún no activa): el usuario la escanea y confirma con un código."""
    secret = totp.new_secret()
    org_name = (await session.get(Organization, agent.organization_id)).name
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, agent.email, org_name or "WA Agent"),
            "setup_token": crypto.seal({"agent_id": agent.id, "secret": secret}, 600)}


class TotpEnableIn(BaseModel):
    setup_token: str
    code: str


@router.post("/auth/mfa/totp/enable")
async def totp_enable(body: TotpEnableIn, request: Request, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    data = crypto.unseal(body.setup_token)
    if not data or data.get("agent_id") != agent.id:
        raise HTTPException(422, "La configuración venció: vuelve a empezar")
    if not totp.verify(data["secret"], body.code):
        raise HTTPException(422, "Código incorrecto: revisa la hora de tu teléfono")
    agent.mfa_secret_id = await put_secret(session, data["secret"], f"mfa_totp:{agent.id}", agent.mfa_secret_id)
    agent.mfa_method, agent.mfa_enabled_at = "totp", utcnow()
    codes = mfa.new_recovery_codes(agent)
    await session.commit()
    await audit.record("mfa_enabled", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, method="totp")
    return {"enabled": True, "method": "totp", "recovery_codes": codes}


@router.post("/auth/mfa/email/enable")
async def email_mfa_enable(request: Request, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    if not get_settings().smtp_host:
        raise HTTPException(409, "El servidor no tiene correo configurado: usa una app autenticadora")
    agent.mfa_method, agent.mfa_enabled_at = "email", utcnow()
    codes = mfa.new_recovery_codes(agent)
    await session.commit()
    await audit.record("mfa_enabled", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, method="email")
    return {"enabled": True, "method": "email", "recovery_codes": codes}


class PasswordIn(BaseModel):
    password: str


@router.post("/auth/mfa/disable")
async def mfa_disable(body: PasswordIn, request: Request, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    pol = await sec_policy.get_policy(session, agent.organization_id)
    if sec_policy.mfa_required(agent, pol):
        raise HTTPException(409, "Tu empresa exige el segundo factor: no puedes desactivarlo")
    if agent.password_hash and not verify_password(body.password, agent.password_hash):
        raise HTTPException(422, "La contraseña no es correcta")
    await _clear_mfa(session, agent)
    await session.commit()
    await audit.record("mfa_disabled", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email)
    return {"enabled": False}


@router.post("/auth/mfa/recovery-codes")
async def regenerate_recovery(body: PasswordIn, agent: Agent = Depends(current_agent),
                              session: AsyncSession = Depends(get_session)):
    if not agent.mfa_method:
        raise HTTPException(409, "Activa primero el segundo factor")
    if agent.password_hash and not verify_password(body.password, agent.password_hash):
        raise HTTPException(422, "La contraseña no es correcta")
    codes = mfa.new_recovery_codes(agent)
    await session.commit()
    return {"recovery_codes": codes}


@router.get("/auth/devices")
async def my_devices(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(TrustedDevice).where(TrustedDevice.agent_id == agent.id,
                                                              TrustedDevice.expires_at > utcnow())
                                  .order_by(TrustedDevice.last_used_at.desc()))).all()
    return [{"id": d.id, "ip": d.ip, "user_agent": d.user_agent, "last_used_at": d.last_used_at,
             "expires_at": d.expires_at} for d in rows]


@router.delete("/auth/devices")
async def forget_my_devices(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await mfa.forget_devices(session, agent.id)
    await session.commit()
    return {"ok": True}


async def _clear_mfa(session: AsyncSession, agent: Agent) -> None:
    if agent.mfa_secret_id:
        await delete_secret(session, agent.mfa_secret_id)
    agent.mfa_method = agent.mfa_secret_id = agent.mfa_enabled_at = None
    agent.mfa_recovery_hashes = []
    await mfa.forget_devices(session, agent.id)


# --- Recuperación de contraseña ---------------------------------------------------------------------------------
class ForgotIn(BaseModel):
    email: str


@router.post("/auth/forgot")
async def forgot(body: ForgotIn, request: Request, session: AsyncSession = Depends(get_session)):
    """Siempre responde lo mismo (no revela si el correo existe). Enlace de un solo uso, 30 minutos."""
    email = body.email.strip().lower()
    ip = audit.client_ip(request)
    if not await ratelimit.allow(session, f"forgot:{ip}:{email}", 3, 3600) or \
            not await ratelimit.allow(session, f"forgot:ip:{ip}", 20, 3600):
        return {"ok": True}
    if await sso.enforced_for(session, email):
        return {"ok": True}  # la contraseña la gestiona el proveedor de identidad
    agents = (await session.scalars(select(Agent).where(func.lower(Agent.email) == email, Agent.is_active))).all()
    for agent in agents:
        await _send_reset(session, agent, request)
    await audit.record("password_reset_requested", request, email=email, matched=len(agents))
    return {"ok": True}


async def _send_reset(session: AsyncSession, agent: Agent, request: Request | None) -> tuple[str, bool]:
    raw = crypto.random_token()
    await session.execute(update(PasswordReset).where(PasswordReset.agent_id == agent.id,
                                                      PasswordReset.used_at.is_(None)).values(expires_at=utcnow()))
    session.add(PasswordReset(agent_id=agent.id, token_hash=crypto.sha256(raw), ip=audit.client_ip(request),
                              expires_at=utcnow() + RESET_TTL))
    await session.commit()
    link = f"{get_settings().frontend_base_url.rstrip('/')}/recuperar/{raw}"
    from app.onboarding.invites import send_email

    sent = await send_email(agent.email, "Restablece tu contraseña",
                            f"Para crear una nueva contraseña abre este enlace (vence en 30 minutos):\n{link}\n\n"
                            "Si no lo pediste, ignora este mensaje.")
    return link, sent


async def _reset_row(session: AsyncSession, token: str) -> PasswordReset | None:
    row = await session.scalar(select(PasswordReset).where(PasswordReset.token_hash == crypto.sha256(token or "")))
    if not row or row.used_at or row.expires_at <= utcnow():
        return None
    return row


@router.get("/auth/reset/{token}")
async def reset_info(token: str, session: AsyncSession = Depends(get_session)):
    row = await _reset_row(session, token)
    if not row:
        raise HTTPException(410, "El enlace venció o ya se usó: pide uno nuevo")
    agent = await session.get(Agent, row.agent_id)
    pol = await sec_policy.get_policy(session, agent.organization_id)
    return {"email": _mask(agent.email), "policy": {k: pol[k] for k in ("min_length", "require_upper", "require_lower",
                                                                        "require_digit", "require_symbol")}}


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/auth/reset")
async def reset_password(body: ResetIn, request: Request, session: AsyncSession = Depends(get_session)):
    row = await _reset_row(session, body.token)
    if not row:
        raise HTTPException(410, "El enlace venció o ya se usó: pide uno nuevo")
    agent = await session.get(Agent, row.agent_id)
    if not agent or not agent.is_active:
        raise HTTPException(410, "El enlace venció o ya se usó: pide uno nuevo")
    errors = await sec_policy.validate_new_password(session, agent, body.password)
    if errors:
        raise HTTPException(422, {"message": "La contraseña no cumple la política", "errors": errors})
    await sec_policy.set_password(session, agent, body.password)
    row.used_at = utcnow()
    await mfa.forget_devices(session, agent.id)
    await session.commit()
    await audit.record("password_reset", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email)
    return {"ok": True}


def _mask(email: str) -> str:
    user, _, domain = email.partition("@")
    return (user[:2] + "***@" + domain) if domain else "***"


# --- Administración: política, usuarios, auditoría -------------------------------------------------------------
@router.get("/security/policy")
async def get_policy(agent: Agent = Depends(require_permission("security.manage")),
                     session: AsyncSession = Depends(get_session)):
    return await sec_policy.get_policy(session, agent.organization_id)


@router.put("/security/policy")
async def put_policy(body: dict, request: Request, agent: Agent = Depends(require_permission("security.manage")),
                     session: AsyncSession = Depends(get_session)):
    try:
        clean = sec_policy.sanitize(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if clean.get("allowed_ips") and not sec_policy.ip_allowed(audit.client_ip(request), clean["allowed_ips"]):
        raise HTTPException(422, "Tu IP actual no está en la lista: te quedarías sin acceso")
    out = await set_setting(session, "security", clean, agent.organization_id, agent_id=agent.id)
    await audit.record("role_changed", request, organization_id=agent.organization_id, agent_id=agent.id,
                       email=agent.email, change="security_policy")
    return out


async def _org_agent(session: AsyncSession, admin: Agent, agent_id: int) -> Agent:
    a = await session.get(Agent, agent_id)
    if not a or a.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    return a


@router.post("/security/users/{agent_id}/unlock")
async def unlock(agent_id: int, request: Request, admin: Agent = Depends(require_permission("users.manage")),
                 session: AsyncSession = Depends(get_session)):
    a = await _org_agent(session, admin, agent_id)
    a.failed_logins, a.locked_until = 0, None
    await session.commit()
    await audit.record("unlocked", request, organization_id=a.organization_id, agent_id=a.id, email=a.email,
                       by=admin.id)
    return {"ok": True}


@router.post("/security/users/{agent_id}/mfa/reset")
async def reset_user_mfa(agent_id: int, request: Request, admin: Agent = Depends(require_permission("security.manage")),
                         session: AsyncSession = Depends(get_session)):
    a = await _org_agent(session, admin, agent_id)
    await _clear_mfa(session, a)
    await session.commit()
    await audit.record("mfa_disabled", request, organization_id=a.organization_id, agent_id=a.id, email=a.email,
                       by=admin.id)
    return {"ok": True}


@router.post("/security/users/{agent_id}/force-reset")
async def force_reset(agent_id: int, request: Request, admin: Agent = Depends(require_permission("users.manage")),
                      session: AsyncSession = Depends(get_session)):
    """Obliga a cambiar la contraseña en el próximo ingreso y envía (o devuelve) un enlace de recuperación."""
    a = await _org_agent(session, admin, agent_id)
    a.must_change_password = True
    link, sent = await _send_reset(session, a, request)
    await audit.record("password_reset_requested", request, organization_id=a.organization_id, agent_id=a.id,
                       email=a.email, by=admin.id)
    return {"ok": True, "email_sent": sent, "link": None if sent else link}


@router.get("/security/audit")
async def audit_events(event: str | None = None, agent_id: int | None = None, email: str | None = None,
                       ip: str | None = None, days: int = 30, limit: int = 100, before_id: int | None = None,
                       admin: Agent = Depends(require_permission("audit.view")),
                       session: AsyncSession = Depends(get_session)):
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    org = admin.organization_id
    # Eventos de la empresa + intentos fallidos con correos de sus usuarios (aún sin empresa resuelta)
    emails = select(func.lower(Agent.email)).where(Agent.organization_id == org).scalar_subquery()
    cond = [AuthEvent.created_at >= since,
            (AuthEvent.organization_id == org) | (and_(AuthEvent.organization_id.is_(None),
                                                       func.lower(AuthEvent.email).in_(emails)))]
    if event:
        cond.append(AuthEvent.event == event)
    if agent_id:
        cond.append(AuthEvent.agent_id == agent_id)
    if email:
        cond.append(func.lower(AuthEvent.email).contains(email.lower()))
    if ip:
        cond.append(AuthEvent.ip == ip)
    if before_id:
        cond.append(AuthEvent.id < before_id)
    rows = (await session.scalars(select(AuthEvent).where(*cond).order_by(AuthEvent.id.desc())
                                  .limit(min(max(limit, 1), 500)))).all()
    names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
    return [{"id": r.id, "created_at": r.created_at, "event": r.event, "agent_id": r.agent_id,
             "agent_name": names.get(r.agent_id), "email": r.email, "ip": r.ip, "user_agent": r.user_agent,
             "detail": r.detail} for r in rows]


@router.get("/security/overview")
async def overview(admin: Agent = Depends(require_permission("security.manage")),
                   session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    agents = (await session.scalars(select(Agent).where(Agent.organization_id == org, Agent.is_active))).all()
    now = utcnow()
    return {"users": len(agents), "mfa_enabled": sum(1 for a in agents if a.mfa_method),
            "locked": [{"id": a.id, "name": a.name, "email": a.email, "locked_until": a.locked_until}
                       for a in agents if a.locked_until and a.locked_until > now],
            "must_change_password": sum(1 for a in agents if a.must_change_password),
            "sso_users": sum(1 for a in agents if not a.password_hash)}


# --- Conexiones SSO (administración) -------------------------------------------------------------------------
class SSOIn(BaseModel):
    protocol: str
    name: str
    slug: str | None = None
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_certificate: str | None = None
    idp_metadata_url: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # vacío = conservar
    scopes: list[str] | None = None
    domains: list[str] = []
    jit_provisioning: bool = True
    default_role_id: int | None = None
    group_claim: str | None = None
    group_mapping: dict = {}
    enforce: bool = False
    is_active: bool = False


def _sso_out(c: SSOConnection) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {"id": c.id, "protocol": c.protocol, "name": c.name, "slug": c.slug, "idp_entity_id": c.idp_entity_id,
            "idp_sso_url": c.idp_sso_url, "idp_certificate": c.idp_certificate, "idp_metadata_url": c.idp_metadata_url,
            "issuer": c.issuer, "client_id": c.client_id, "has_client_secret": bool(c.client_secret_secret_id),
            "scopes": c.scopes, "domains": c.domains, "jit_provisioning": c.jit_provisioning,
            "default_role_id": c.default_role_id, "group_claim": c.group_claim, "group_mapping": c.group_mapping,
            "enforce": c.enforce, "is_active": c.is_active, "last_login_at": c.last_login_at,
            "sp": {"entity_id": sso.sp_entity_id(c), "acs_url": sso.acs_url(c),
                   "metadata_url": f"{base}/auth/sso/{c.slug}/metadata", "oidc_redirect_uri": sso.oidc_redirect_uri(c),
                   "login_url": f"{base}/auth/sso/{c.slug}/start"}}


async def _apply_sso(session: AsyncSession, admin: Agent, c: SSOConnection, body: SSOIn) -> None:
    if body.protocol not in ("saml", "oidc"):
        raise HTTPException(422, "Protocolo inválido (saml | oidc)")
    for k in ("protocol", "name", "idp_entity_id", "idp_sso_url", "idp_certificate", "idp_metadata_url", "issuer",
              "client_id", "jit_provisioning", "group_claim", "group_mapping", "enforce", "is_active"):
        setattr(c, k, getattr(body, k))
    c.scopes = body.scopes or ["openid", "email", "profile"]
    c.domains = sorted({d.strip().lower().lstrip("@") for d in body.domains if d.strip()})
    if body.default_role_id is not None:
        role = await session.get(Role, body.default_role_id)
        if not role or role.organization_id != admin.organization_id:
            raise HTTPException(422, "Rol por defecto inválido")
    c.default_role_id = body.default_role_id
    taken = await session.scalar(select(SSOConnection.organization_id).where(
        SSOConnection.is_active, SSOConnection.organization_id != admin.organization_id,
        SSOConnection.domains.overlap(c.domains)))
    if c.is_active and taken:
        raise HTTPException(409, "Uno de esos dominios ya usa SSO en otra empresa")
    if body.client_secret:
        c.client_secret_secret_id = await put_secret(session, body.client_secret, f"sso_client_secret:{c.slug}",
                                                     c.client_secret_secret_id)
    if c.protocol == "saml" and c.idp_metadata_url and not (c.idp_sso_url and c.idp_certificate):
        try:
            await sso.import_idp_metadata(c)
        except sso.SSOError as e:
            raise HTTPException(422, str(e)) from None
    if c.is_active:
        if c.protocol == "saml" and not (c.idp_sso_url and c.idp_certificate):
            raise HTTPException(422, "SAML: indica la URL de inicio de sesión y el certificado del proveedor")
        if c.protocol == "oidc" and not (c.issuer and c.client_id):
            raise HTTPException(422, "OIDC: indica el emisor (issuer) y el client id")


@router.get("/security/sso")
async def list_sso(admin: Agent = Depends(require_permission("security.manage")),
                   session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(SSOConnection).where(
        SSOConnection.organization_id == admin.organization_id).order_by(SSOConnection.id))).all()
    return [_sso_out(c) for c in rows]


@router.post("/security/sso")
async def create_sso(body: SSOIn, request: Request, admin: Agent = Depends(require_permission("security.manage")),
                     session: AsyncSession = Depends(get_session)):
    slug = (body.slug or "").strip().lower() or f"{body.protocol}-{secrets.token_hex(4)}"
    if not all(ch.isalnum() or ch == "-" for ch in slug) or len(slug) > 60:
        raise HTTPException(422, "Slug inválido")
    if await session.scalar(select(SSOConnection.id).where(SSOConnection.slug == slug)):
        raise HTTPException(409, "Ese identificador ya existe")
    c = SSOConnection(organization_id=admin.organization_id, slug=slug, protocol=body.protocol, name=body.name)
    session.add(c)
    await _apply_sso(session, admin, c, body)
    await session.commit()
    await audit.record("role_changed", request, organization_id=admin.organization_id, agent_id=admin.id,
                       email=admin.email, change="sso_created", slug=slug)
    return _sso_out(c)


async def _sso(session: AsyncSession, admin: Agent, cid: int) -> SSOConnection:
    c = await session.get(SSOConnection, cid)
    if not c or c.organization_id != admin.organization_id:
        raise HTTPException(404, "Conexión no encontrada")
    return c


@router.put("/security/sso/{cid}")
async def update_sso(cid: int, body: SSOIn, request: Request,
                     admin: Agent = Depends(require_permission("security.manage")),
                     session: AsyncSession = Depends(get_session)):
    c = await _sso(session, admin, cid)
    await _apply_sso(session, admin, c, body)
    await session.commit()
    await audit.record("role_changed", request, organization_id=admin.organization_id, agent_id=admin.id,
                       email=admin.email, change="sso_updated", slug=c.slug)
    return _sso_out(c)


@router.delete("/security/sso/{cid}")
async def delete_sso(cid: int, admin: Agent = Depends(require_permission("security.manage")),
                     session: AsyncSession = Depends(get_session)):
    c = await _sso(session, admin, cid)
    await delete_secret(session, c.client_secret_secret_id)
    await session.delete(c)
    await session.commit()
    return {"ok": True}


@router.post("/security/sso/{cid}/test")
async def test_sso(cid: int, admin: Agent = Depends(require_permission("security.manage")),
                   session: AsyncSession = Depends(get_session)):
    """Comprueba la configuración del proveedor sin iniciar sesión (metadata OIDC / SAML)."""
    c = await _sso(session, admin, cid)
    try:
        if c.protocol == "oidc":
            url = await sso.oidc_start(c)
            if c.client_secret_secret_id:
                await get_secret(session, c.client_secret_secret_id)
            return {"ok": True, "detail": "Configuración OIDC leída correctamente", "authorize_url": url.split("?")[0]}
        if c.idp_metadata_url:
            await sso.import_idp_metadata(c)
            await session.commit()
        if not (c.idp_sso_url and c.idp_certificate):
            return {"ok": False, "detail": "Faltan la URL de inicio de sesión o el certificado del proveedor"}
        sso._pem(c.idp_certificate)
        return {"ok": True, "detail": "Metadata SAML válida", "idp_sso_url": c.idp_sso_url}
    except sso.SSOError as e:
        return {"ok": False, "detail": str(e)}


@router.post("/security/sessions/revoke-challenges")
async def revoke_pending_challenges(admin: Agent = Depends(require_permission("security.manage")),
                                    session: AsyncSession = Depends(get_session)):
    """Invalida los retos de segundo factor pendientes de la empresa (p. ej. tras un incidente)."""
    ids = select(Agent.id).where(Agent.organization_id == admin.organization_id).scalar_subquery()
    await session.execute(update(MfaChallenge).where(MfaChallenge.agent_id.in_(ids), MfaChallenge.verified_at.is_(None))
                          .values(expires_at=utcnow()))
    await session.commit()
    return {"ok": True}
