"""Alta de empresas (SaaS) y conexión de números de WhatsApp con Meta Embedded Signup."""

import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_token, hash_password, require_admin
from app.config import get_settings
from app.db import get_session
from app.fields import EMAIL_RE
from app.models import Agent, AIAgent, Channel, Organization, Plan
from app.plans import enforce_limit
from app.schemas import AgentOut
from app.secrets_vault import put_secret
from app.tenancy import create_org, plan_by_key, provision_ai_defaults

router = APIRouter(prefix="/api", tags=["signup"])
settings = get_settings()

SIGNUP_LIMIT, SIGNUP_WINDOW_S = 5, 3600
_signups: defaultdict[str, deque] = defaultdict(deque)


class SignupIn(BaseModel):
    company: str
    name: str
    email: str
    password: str
    country: str | None = None
    plan_key: str | None = None


class EmbeddedSignupIn(BaseModel):
    code: str
    phone_number_id: str
    waba_id: str
    name: str | None = None
    pin: str | None = None  # PIN de dos pasos (6 dígitos) si el número aún no está registrado


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


def _rate_limit(ip: str) -> None:
    now = time.monotonic()
    q = _signups[ip]
    while q and now - q[0] > SIGNUP_WINDOW_S:
        q.popleft()
    if len(q) >= SIGNUP_LIMIT:
        raise HTTPException(429, "Demasiados registros desde esta red. Intenta más tarde.")
    q.append(now)


@router.get("/signup/config")
async def signup_config(session: AsyncSession = Depends(get_session)):
    """Público: si el registro está abierto y qué planes se pueden elegir."""
    plans = (await session.scalars(select(Plan).where(Plan.is_public).order_by(Plan.position))).all()
    return {"enabled": settings.saas_signup_enabled, "trial_days": settings.saas_trial_days,
            "default_plan": settings.saas_default_plan,
            "plans": [{"key": p.key, "name": p.name, "price_month_usd": float(p.price_month_usd),
                       "limits": p.limits, "features": p.features} for p in plans]}


@router.post("/signup")
async def signup(body: SignupIn, request: Request, session: AsyncSession = Depends(get_session)):
    if not settings.saas_signup_enabled:
        raise HTTPException(403, "El registro de nuevas empresas está cerrado")
    _rate_limit(_client_ip(request))
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Correo inválido")
    if len(body.password) < 8:
        raise HTTPException(422, "La contraseña debe tener al menos 8 caracteres")
    if len(body.company.strip()) < 2 or not body.name.strip():
        raise HTTPException(422, "Indica el nombre de la empresa y tu nombre")
    if body.plan_key:
        plan = await plan_by_key(session, body.plan_key)
        if not plan or not plan.is_public:
            raise HTTPException(422, "Plan inválido")

    org = await create_org(session, body.company, body.country, body.plan_key, status="trial", billing_email=email)
    admin = Agent(organization_id=org.id, email=email, name=body.name.strip(),
                  password_hash=hash_password(body.password), role="admin")
    session.add(admin)
    await session.flush()
    await provision_ai_defaults(session, org.id)
    await session.commit()
    return {"access_token": create_token(admin), "agent": AgentOut.model_validate(admin).model_dump(),
            "organization": {"id": org.id, "name": org.name, "status": org.status, "trial_ends_at": org.trial_ends_at}}


# --- Meta Embedded Signup -------------------------------------------------------
@router.get("/channels/embedded-signup/config")
async def embedded_config(_: Agent = Depends(require_admin)):
    return {"app_id": settings.meta_app_id or None, "config_id": settings.meta_embedded_signup_config_id or None,
            "api_version": settings.wa_api_version,
            "enabled": bool(settings.meta_app_id and settings.meta_app_secret and settings.meta_embedded_signup_config_id)}


async def _graph(method: str, path: str, token: str | None = None, **kw) -> dict:
    url = f"https://graph.facebook.com/{settings.wa_api_version}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(method, url, headers=headers, **kw)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise HTTPException(502, f"Meta: {msg}")
    return data


@router.post("/channels/embedded-signup")
async def embedded_signup(body: EmbeddedSignupIn, admin: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Conecta el número que el cliente autorizó en el flujo de Embedded Signup de Meta."""
    if not (settings.meta_app_id and settings.meta_app_secret):
        raise HTTPException(409, "Falta configurar META_APP_ID y META_APP_SECRET")
    existing = await session.scalar(select(Channel).where(Channel.phone_number_id == body.phone_number_id))
    if existing and existing.organization_id != admin.organization_id:
        raise HTTPException(409, "Ese número ya está conectado a otra empresa")
    if not existing:
        await enforce_limit(session, admin.organization_id, "channels")

    token_data = await _graph("GET", "oauth/access_token", params={
        "client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": body.code})
    token = token_data.get("access_token")
    if not token:
        raise HTTPException(502, "Meta no devolvió un token")
    await _graph("POST", f"{body.waba_id}/subscribed_apps", token)
    if body.pin:
        await _graph("POST", f"{body.phone_number_id}/register", token,
                     json={"messaging_product": "whatsapp", "pin": body.pin})
    info = await _graph("GET", body.phone_number_id, token, params={"fields": "display_phone_number,verified_name"})

    channel = existing or Channel(organization_id=admin.organization_id, phone_number_id=body.phone_number_id,
                                  name=body.name or info.get("verified_name") or "WhatsApp")
    if not existing:
        channel.default_ai_agent_id = await session.scalar(select(AIAgent.id).where(
            AIAgent.organization_id == admin.organization_id).order_by(AIAgent.id).limit(1))
        session.add(channel)
        await session.flush()
    channel.waba_id = body.waba_id
    channel.display_phone = info.get("display_phone_number")
    channel.access_token_secret_id = await put_secret(session, token, f"channel:{channel.id}",
                                                      channel.access_token_secret_id)
    await session.commit()
    org = await session.get(Organization, admin.organization_id)
    return {"id": channel.id, "name": channel.name, "phone_number_id": channel.phone_number_id,
            "display_phone": channel.display_phone, "waba_id": channel.waba_id, "organization": org.name}
