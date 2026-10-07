"""Suscripciones Web Push del asesor conectado (app instalable)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.config import get_settings
from app.db import get_session
from app.models import Agent, PushSubscription

router = APIRouter(prefix="/api/push", tags=["push"])
PREFERENCES = ("assigned", "message_assigned", "call", "new_conversation")


class Keys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: Keys
    preferences: dict[str, bool] | None = None


class EndpointIn(BaseModel):
    endpoint: str


class PreferencesIn(BaseModel):
    endpoint: str
    preferences: dict[str, bool]


def _prefs(raw: dict[str, bool] | None) -> dict[str, bool]:
    base = {"assigned": True, "message_assigned": True, "call": True, "new_conversation": False}
    return {**base, **{k: bool(v) for k, v in (raw or {}).items() if k in PREFERENCES}}


@router.get("/vapid-public-key")
async def vapid_public_key(_: Agent = Depends(current_agent)):
    key = get_settings().vapid_public_key
    return {"enabled": bool(key and get_settings().vapid_private_key), "public_key": key or None}


@router.get("/subscriptions")
async def my_subscriptions(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(PushSubscription).where(PushSubscription.agent_id == agent.id))).all()
    return [{"id": s.id, "endpoint": s.endpoint, "user_agent": s.user_agent, "preferences": s.preferences,
             "last_success_at": s.last_success_at, "created_at": s.created_at} for s in rows]


@router.post("/subscriptions")
async def subscribe(body: SubscriptionIn, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session), user_agent: str | None = None):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(422, "Suscripción inválida")
    sub = await session.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint)
        session.add(sub)
    # El mismo navegador puede cambiar de asesor (otro inicio de sesión): la suscripción pasa al actual
    sub.organization_id, sub.agent_id = agent.organization_id, agent.id
    sub.p256dh, sub.auth, sub.failures = body.keys.p256dh, body.keys.auth, 0
    sub.preferences = _prefs(body.preferences or sub.preferences)
    sub.user_agent = (user_agent or "")[:300] or sub.user_agent
    await session.commit()
    return {"id": sub.id, "preferences": sub.preferences}


@router.put("/subscriptions/preferences")
async def preferences(body: PreferencesIn, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    sub = await session.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                              PushSubscription.agent_id == agent.id))
    if not sub:
        raise HTTPException(404, "Suscripción no encontrada")
    sub.preferences = _prefs({**(sub.preferences or {}), **body.preferences})
    await session.commit()
    return {"id": sub.id, "preferences": sub.preferences}


@router.delete("/subscriptions")
async def unsubscribe(body: EndpointIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                         PushSubscription.agent_id == agent.id))
    await session.commit()
    return {"ok": True}


@router.post("/test")
async def test_push(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Aviso de prueba a todos los dispositivos del asesor."""
    from app import push

    if not push.enabled():
        raise HTTPException(409, "Las notificaciones no están configuradas en el servidor (VAPID_PUBLIC_KEY/PRIVATE_KEY)")
    sent = await push.send(session, agent.organization_id, [agent.id], None, {
        "title": "Notificaciones activas", "body": "Así te avisaremos de conversaciones y llamadas.", "url": "/"})
    return {"sent": sent}
