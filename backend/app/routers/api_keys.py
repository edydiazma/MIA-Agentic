"""Panel: llaves de la API pública (solo administradores, plan con «api»). La llave completa se muestra una vez."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import Agent, ApiKey, OutboundWebhook, utcnow
from app.plans import feature_required
from app.public_api.keys import SCOPES, clean_scopes, new_key
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"], dependencies=[Depends(feature_required("api"))])


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1)
    rate_limit_per_min: int = Field(default=120, ge=1, le=6000)
    expires_at: datetime | None = None


class KeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    scopes: list[str]
    rate_limit_per_min: int
    last_used_at: UTCDateTime | None
    last_used_ip: str | None
    expires_at: UTCDateTime | None
    revoked_at: UTCDateTime | None
    created_at: UTCDateTime
    webhooks: int = 0
    requests_30d: int = 0
    key: str | None = None  # solo al crear o rotar


def _out(k: ApiKey, key: str | None = None, webhooks: int = 0, requests: int = 0) -> KeyOut:
    return KeyOut(id=k.id, name=k.name, prefix=k.prefix, scopes=list(k.scopes or []),
                  rate_limit_per_min=k.rate_limit_per_min, last_used_at=k.last_used_at, last_used_ip=k.last_used_ip,
                  expires_at=k.expires_at, revoked_at=k.revoked_at, created_at=k.created_at, webhooks=webhooks,
                  requests_30d=requests, key=key)


async def _get(session: AsyncSession, org: int, key_id: int) -> ApiKey:
    k = await session.get(ApiKey, key_id)
    if not k or k.organization_id != org:
        raise HTTPException(404, "Llave no encontrada")
    return k


def _scopes(scopes: list[str]) -> list[str]:
    try:
        return clean_scopes(scopes)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/scopes")
async def list_scopes(_: Agent = Depends(require_admin)):
    return [{"key": k, "label": v} for k, v in SCOPES.items()]


@router.get("", response_model=list[KeyOut])
async def list_keys(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    keys = (await session.scalars(select(ApiKey).where(ApiKey.organization_id == org)
                                  .order_by(ApiKey.revoked_at.is_not(None), ApiKey.id.desc()))).all()
    hooks = dict((await session.execute(text(
        "select api_key_id, count(*) from public.outbound_webhooks where organization_id = :o and api_key_id is not null "
        "group by api_key_id"), {"o": org})).all())
    usage = dict((await session.execute(text(
        "select api_key_id, sum(requests)::int from reporting.daily_api where organization_id = :o "
        "and day >= current_date - 29 group by api_key_id"), {"o": org})).all())
    return [_out(k, webhooks=hooks.get(k.id, 0), requests=usage.get(k.id, 0)) for k in keys]


@router.post("", response_model=KeyOut)
async def create_key(body: KeyIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    full, prefix, digest = new_key()
    k = ApiKey(organization_id=agent.organization_id, name=body.name.strip(), prefix=prefix, key_hash=digest,
               scopes=_scopes(body.scopes), rate_limit_per_min=body.rate_limit_per_min, expires_at=body.expires_at,
               created_by=agent.id)
    session.add(k)
    await session.commit()
    return _out(k, key=full)


@router.put("/{key_id}", response_model=KeyOut)
async def update_key(key_id: int, body: KeyIn, agent: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    k = await _get(session, agent.organization_id, key_id)
    k.name, k.scopes = body.name.strip(), _scopes(body.scopes)
    k.rate_limit_per_min, k.expires_at = body.rate_limit_per_min, body.expires_at
    await session.commit()
    return _out(k)


@router.post("/{key_id}/rotate", response_model=KeyOut)
async def rotate_key(key_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Nueva llave con los mismos alcances; la anterior deja de funcionar de inmediato."""
    k = await _get(session, agent.organization_id, key_id)
    if k.revoked_at:
        raise HTTPException(409, "La llave está revocada")
    full, prefix, digest = new_key()
    k.prefix, k.key_hash = prefix, digest
    await session.commit()
    return _out(k, key=full)


@router.post("/{key_id}/revoke", response_model=KeyOut)
async def revoke_key(key_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Revoca la llave; sus suscripciones de webhooks (Zapier, Make, n8n) se desactivan."""
    from app.webhooks_out import invalidate_cache

    k = await _get(session, agent.organization_id, key_id)
    k.revoked_at = k.revoked_at or utcnow()
    for w in (await session.scalars(select(OutboundWebhook).where(OutboundWebhook.api_key_id == k.id))).all():
        w.active = False
    await session.commit()
    invalidate_cache()
    return _out(k)
