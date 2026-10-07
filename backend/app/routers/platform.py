"""Back-office del dueño de la plataforma: empresas, planes, consumo, suspensiones y métricas globales.

Usa su propio JWT (audiencia "platform"); los tokens de asesores no sirven aquí y viceversa.
"""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, verify_password
from app.config import get_settings
from app.db import get_session
from app.models import (
    Agent,
    Channel,
    Conversation,
    Organization,
    Plan,
    PlatformAdmin,
    Subscription,
    UsageCounter,
    utcnow,
)
from app.plans import FEATURES, LIMIT_METRICS, month_start, plan_status
from app.tenancy import trial_days_left

router = APIRouter(prefix="/api/platform", tags=["platform"])
settings = get_settings()
bearer = HTTPBearer(auto_error=False)
AUDIENCE = "platform"
ORG_STATUSES = ("trial", "active", "past_due", "suspended", "cancelled")


class LoginIn(BaseModel):
    email: str
    password: str


class OrgUpdate(BaseModel):
    status: str | None = None
    plan_id: int | None = None
    extend_trial_days: int | None = None
    name: str | None = None


class PlanIn(BaseModel):
    key: str | None = None
    name: str | None = None
    description: str | None = None
    price_month_usd: float | None = None
    limits: dict | None = None
    features: dict | None = None
    provider_price_id: str | None = None
    is_public: bool | None = None
    position: int | None = None


def _token(admin: PlatformAdmin) -> str:
    return jwt.encode({"sub": str(admin.id), "aud": AUDIENCE,
                       "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)},
                      settings.jwt_secret, algorithm="HS256")


async def ensure_first_admin(session: AsyncSession) -> None:
    """Crea el primer administrador desde PLATFORM_ADMIN_EMAIL/PASSWORD si la tabla está vacía."""
    if settings.platform_admin_email and settings.platform_admin_password and \
            not await session.scalar(select(PlatformAdmin.id).limit(1)):
        session.add(PlatformAdmin(email=settings.platform_admin_email.lower().strip(), name="Administrador plataforma",
                                  password_hash=hash_password(settings.platform_admin_password)))
        await session.commit()


async def platform_admin(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                         session: AsyncSession = Depends(get_session)) -> PlatformAdmin:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"], audience=AUDIENCE)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido") from None
    admin = await session.get(PlatformAdmin, int(payload["sub"]))
    if not admin:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Administrador no encontrado")
    return admin


def _plan_out(p: Plan) -> dict:
    return {"id": p.id, "key": p.key, "name": p.name, "description": p.description,
            "price_month_usd": float(p.price_month_usd), "limits": p.limits, "features": p.features,
            "provider_price_id": p.provider_price_id, "is_public": p.is_public, "position": p.position}


async def _usage(session: AsyncSession, org_ids: list[int]) -> dict[int, dict]:
    rows = (await session.execute(select(UsageCounter.organization_id, UsageCounter.metric, UsageCounter.value)
                                  .where(UsageCounter.organization_id.in_(org_ids or [0]),
                                         UsageCounter.period == month_start()))).all()
    out: dict[int, dict] = {}
    for org, metric, value in rows:
        out.setdefault(org, {})[metric] = float(value)
    return out


async def _counts(session: AsyncSession, model, org_ids: list[int], *where) -> dict[int, int]:
    rows = (await session.execute(select(model.organization_id, func.count()).where(
        model.organization_id.in_(org_ids or [0]), *where).group_by(model.organization_id))).all()
    return dict(rows)


@router.post("/login")
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    await ensure_first_admin(session)
    admin = await session.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == body.email.lower().strip()))
    if not admin or not verify_password(body.password, admin.password_hash):
        raise HTTPException(401, "Credenciales inválidas")
    return {"access_token": _token(admin), "admin": {"id": admin.id, "email": admin.email, "name": admin.name}}


@router.get("/me")
async def me(admin: PlatformAdmin = Depends(platform_admin)):
    return {"id": admin.id, "email": admin.email, "name": admin.name}


@router.get("/metrics")
async def metrics(_: PlatformAdmin = Depends(platform_admin), session: AsyncSession = Depends(get_session)):
    by_status = dict((await session.execute(select(Organization.status, func.count()).group_by(Organization.status))).all())
    mrr = await session.scalar(select(func.coalesce(func.sum(Plan.price_month_usd), 0)).select_from(Subscription)
                               .join(Plan, Plan.id == Subscription.plan_id)
                               .where(Subscription.status.in_(("active", "past_due")),
                                      Subscription.provider != "manual")) or 0
    by_plan = dict((await session.execute(select(Plan.name, func.count()).select_from(Organization)
                                          .join(Plan, Plan.id == Organization.plan_id)
                                          .group_by(Plan.name))).all())
    conv = await session.scalar(select(func.coalesce(func.sum(UsageCounter.value), 0)).where(
        UsageCounter.period == month_start(), UsageCounter.metric == "conversations")) or 0
    return {"organizations": sum(by_status.values()), "by_status": by_status, "by_plan": by_plan,
            "mrr_usd": float(mrr), "conversations_month": float(conv)}


@router.get("/orgs")
async def list_orgs(q: str | None = None, status_: str | None = Query(default=None, alias="status"),
                    _: PlatformAdmin = Depends(platform_admin), session: AsyncSession = Depends(get_session)):
    stmt = select(Organization).order_by(Organization.created_at.desc()).limit(500)
    if q:
        stmt = stmt.where(Organization.name.ilike(f"%{q}%"))
    if status_:
        stmt = stmt.where(Organization.status == status_)
    orgs = (await session.scalars(stmt)).all()
    ids = [o.id for o in orgs]
    plans = {p.id: p for p in (await session.scalars(select(Plan))).all()}
    usage = await _usage(session, ids)
    users = await _counts(session, Agent, ids, Agent.is_active)
    channels = await _counts(session, Channel, ids)
    subs = {s.organization_id: s for s in (await session.scalars(
        select(Subscription).where(Subscription.organization_id.in_(ids or [0])))).all()}
    return [{
        "id": o.id, "name": o.name, "slug": o.slug, "status": o.status, "country": o.country,
        "plan": _plan_out(plans[o.plan_id]) if o.plan_id in plans else None, "trial_days_left": trial_days_left(o),
        "trial_ends_at": o.trial_ends_at, "created_at": o.created_at, "usage": usage.get(o.id, {}),
        "users": users.get(o.id, 0), "channels": channels.get(o.id, 0),
        "subscription": {"provider": subs[o.id].provider, "status": subs[o.id].status,
                         "current_period_end": subs[o.id].current_period_end} if o.id in subs else None,
    } for o in orgs]


@router.get("/orgs/{org_id}")
async def org_detail(org_id: int, _: PlatformAdmin = Depends(platform_admin),
                     session: AsyncSession = Depends(get_session)):
    org = await session.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "Empresa no encontrada")
    detail = await plan_status(session, org_id)
    admins = (await session.execute(select(Agent.name, Agent.email).where(
        Agent.organization_id == org_id, Agent.role == "admin", Agent.is_active))).all()
    conversations = await session.scalar(select(func.count()).where(Conversation.organization_id == org_id))
    return {**detail, "admins": [{"name": n, "email": e} for n, e in admins], "conversations_total": conversations}


@router.put("/orgs/{org_id}")
async def update_org(org_id: int, body: OrgUpdate, _: PlatformAdmin = Depends(platform_admin),
                     session: AsyncSession = Depends(get_session)):
    org = await session.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "Empresa no encontrada")
    if body.status is not None:
        if body.status not in ORG_STATUSES:
            raise HTTPException(422, f"Estado inválido: {', '.join(ORG_STATUSES)}")
        org.status = body.status
    if body.plan_id is not None:
        if not await session.get(Plan, body.plan_id):
            raise HTTPException(422, "Plan inválido")
        org.plan_id = body.plan_id
        sub = await session.scalar(select(Subscription).where(Subscription.organization_id == org.id))
        if sub and sub.provider == "manual":
            sub.plan_id = body.plan_id
    if body.extend_trial_days:
        base = org.trial_ends_at if org.trial_ends_at and org.trial_ends_at > utcnow() else utcnow()
        org.trial_ends_at = base + timedelta(days=body.extend_trial_days)
        org.status = "trial"
    if body.name:
        org.name = body.name.strip()
    await session.commit()
    return {"id": org.id, "status": org.status, "plan_id": org.plan_id, "trial_ends_at": org.trial_ends_at}


@router.get("/plans")
async def list_plans(_: PlatformAdmin = Depends(platform_admin), session: AsyncSession = Depends(get_session)):
    return [_plan_out(p) for p in (await session.scalars(select(Plan).order_by(Plan.position, Plan.id))).all()]


def _validate_plan(body: PlanIn) -> None:
    for k in (body.limits or {}):
        if k not in LIMIT_METRICS:
            raise HTTPException(422, f"Límite desconocido: {k}")
    for k in (body.features or {}):
        if k not in FEATURES:
            raise HTTPException(422, f"Funcionalidad desconocida: {k}")
    if body.price_month_usd is not None and body.price_month_usd < 0:
        raise HTTPException(422, "El precio no puede ser negativo")


@router.post("/plans")
async def create_plan(body: PlanIn, _: PlatformAdmin = Depends(platform_admin),
                      session: AsyncSession = Depends(get_session)):
    _validate_plan(body)
    if not body.key or not body.name:
        raise HTTPException(422, "Clave y nombre son obligatorios")
    if await session.scalar(select(Plan.id).where(Plan.key == body.key)):
        raise HTTPException(409, "Ya existe un plan con esa clave")
    plan = Plan(**{k: v for k, v in body.model_dump().items() if v is not None})
    session.add(plan)
    await session.commit()
    return _plan_out(plan)


@router.put("/plans/{plan_id}")
async def update_plan(plan_id: int, body: PlanIn, _: PlatformAdmin = Depends(platform_admin),
                      session: AsyncSession = Depends(get_session)):
    plan = await session.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan no encontrado")
    _validate_plan(body)
    data = body.model_dump(exclude_unset=True)
    data.pop("key", None)  # la clave no cambia: la usan suscripciones y metadatos de pago
    for k, v in data.items():
        setattr(plan, k, v)
    await session.commit()
    return _plan_out(plan)
