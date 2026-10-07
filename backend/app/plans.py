"""Planes SaaS: límites y funcionalidades por plan.

Uso desde cualquier router:
    await enforce_limit(session, agent.organization_id, "channels")      # 402 si se alcanzó el límite
    @router.post(..., dependencies=[Depends(feature_required("voice"))])  # 402 si el plan no lo incluye

Límites:
- Duros (bloquean la creación): channels, users, ai_agents, flows, campaign_recipients_month, voice_minutes_month.
- Blando: conversations_month. Superarlo NO deja de atender clientes (sería perder ventas): se crea una alerta
  de facturación una vez al mes y el back-office lo ve como excedente.
"""

import asyncio
import json
import logging
from datetime import UTC, date, datetime

from fastapi import Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.db import SessionLocal, get_session
from app.models import Agent, Alert, Organization, Plan, Subscription

log = logging.getLogger(__name__)

LIMIT_METRICS = ("channels", "users", "ai_agents", "flows", "conversations_month", "voice_minutes_month",
                 "campaign_recipients_month")
FEATURES = ("flows", "voice", "crm", "attribution", "api", "seller", "memory")
METRIC_LABELS = {
    "channels": "números de WhatsApp", "users": "usuarios", "ai_agents": "agentes de IA", "flows": "flujos",
    "conversations_month": "conversaciones del mes", "voice_minutes_month": "minutos de voz del mes",
    "campaign_recipients_month": "destinatarios de campañas del mes",
}
FEATURE_LABELS = {"flows": "Flujos", "voice": "Llamadas y agentes de voz", "crm": "Integraciones CRM",
                  "attribution": "Atribución y conversiones", "api": "API y webhooks", "seller": "Mejor vendedor",
                  "memory": "Memoria del negocio"}


async def check_limit(session: AsyncSession, org: int, metric: str) -> dict:
    """{metric, limit, used, allowed} calculado en la base (public.check_limit)."""
    r = await session.scalar(text("select public.check_limit(:o, :m)"), {"o": org, "m": metric})
    return json.loads(r) if isinstance(r, str) else r


async def remaining(session: AsyncSession, org: int, metric: str) -> float | None:
    """Cupo restante (None = ilimitado)."""
    r = await check_limit(session, org, metric)
    if not r or r.get("limit") is None:
        return None
    return max(0.0, float(r["limit"]) - float(r["used"]))


async def enforce_limit(session: AsyncSession, org: int, metric: str, needed: float = 1) -> None:
    left = await remaining(session, org, metric)
    if left is not None and left < needed:
        r = await check_limit(session, org, metric)
        label = METRIC_LABELS.get(metric, metric)
        raise HTTPException(402, f"Tu plan permite {int(float(r['limit']))} {label} (usados: {int(float(r['used']))}). "
                                 "Actualiza el plan en Plan y facturación para continuar.")


async def has_feature(session: AsyncSession, org: int, feature: str) -> bool:
    o = await session.get(Organization, org)
    if not o or not o.plan_id:
        return True  # instalación sin plan asignado: todo habilitado
    plan = await session.get(Plan, o.plan_id)
    return bool((plan.features or {}).get(feature, False))


def feature_required(feature: str):
    async def dependency(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
        if not await has_feature(session, agent.organization_id, feature):
            label = FEATURE_LABELS.get(feature, feature)
            raise HTTPException(402, f"Tu plan no incluye «{label}». Actualiza el plan para usarlo.")
    return dependency


def month_start() -> date:
    now = datetime.now(UTC)
    return date(now.year, now.month, 1)


async def plan_status(session: AsyncSession, org_id: int) -> dict:
    """Plan, funcionalidades, límites con uso actual, consumo del mes (mensajes, costo de IA) y estado."""
    from app.tenancy import trial_days_left

    org = await session.get(Organization, org_id)
    plan = await session.get(Plan, org.plan_id) if org.plan_id else None
    sub = await session.scalar(select(Subscription).where(Subscription.organization_id == org_id))
    usage = {m: await check_limit(session, org_id, m) for m in LIMIT_METRICS}
    start = month_start()
    messages_out = await session.scalar(text(
        "select coalesce(sum(messages), 0) from reporting.hourly_messages "
        "where organization_id = :o and direction = 'out' and hour >= :s"), {"o": org_id, "s": start})
    ai = (await session.execute(text(
        "select coalesce(sum(calls), 0), coalesce(sum(cost_usd), 0), coalesce(sum(input_tokens + output_tokens), 0) "
        "from reporting.daily_ai where organization_id = :o and day >= :s"), {"o": org_id, "s": start})).one()
    return {
        "organization": {"id": org.id, "name": org.name, "status": org.status, "trial_ends_at": org.trial_ends_at,
                         "trial_days_left": trial_days_left(org), "country": org.country},
        "plan": {"id": plan.id, "key": plan.key, "name": plan.name, "price_month_usd": float(plan.price_month_usd),
                 "features": plan.features, "limits": plan.limits} if plan else None,
        "subscription": {"provider": sub.provider, "status": sub.status, "current_period_end": sub.current_period_end,
                         "cancel_at_period_end": sub.cancel_at_period_end,
                         "has_customer": bool(sub.provider_customer_id)} if sub else None,
        "usage": {m: {"used": float(u["used"]), "limit": None if u["limit"] is None else float(u["limit"]),
                      "allowed": u["allowed"]} for m, u in usage.items()},
        "consumption": {"messages_out": int(messages_out or 0), "ai_calls": int(ai[0]), "ai_cost_usd": float(ai[1]),
                        "ai_tokens": int(ai[2]), "period_start": start.isoformat()},
        "labels": {"metrics": METRIC_LABELS, "features": FEATURE_LABELS},
    }


async def check_soft_limits() -> int:
    """Alerta (una vez al mes) a las empresas que superaron su cupo de conversaciones."""
    created = 0
    period = month_start().strftime("%Y-%m")
    async with SessionLocal() as session:
        orgs = (await session.scalars(select(Organization.id).where(
            Organization.status.in_(("trial", "active", "past_due"))))).all()
        for org in orgs:
            r = await check_limit(session, org, "conversations_month")
            if not r or r.get("allowed", True):
                continue
            ref = f"conversations_month:{period}"
            if await session.scalar(select(Alert.id).where(Alert.organization_id == org, Alert.ref == ref)):
                continue
            session.add(Alert(organization_id=org, severity="warning", layer="billing", source="system", ref=ref,
                              title="Superaste las conversaciones incluidas en tu plan",
                              description=f"Usadas {int(float(r['used']))} de {int(float(r['limit']))} este mes. "
                                          "Se siguen atendiendo; considera subir de plan."))
            created += 1
        await session.commit()
    return created


async def usage_loop() -> None:
    while True:
        await asyncio.sleep(600)
        try:
            await check_soft_limits()
        except Exception:
            log.exception("Falló la revisión de límites del plan")
