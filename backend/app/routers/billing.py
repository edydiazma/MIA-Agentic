"""Plan de la empresa y facturación (Stripe): estado, checkout, portal del cliente y webhook."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.billing.base import BillingError
from app.billing.service import apply_event
from app.billing.stripe import StripeProvider
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models import Agent, BillingEvent, InboundEvent, Organization, Plan, Subscription, utcnow
from app.plans import plan_status

router = APIRouter(prefix="/api", tags=["billing"])
settings = get_settings()
log = logging.getLogger(__name__)


class CheckoutIn(BaseModel):
    plan_key: str


def provider() -> StripeProvider:
    return StripeProvider()


@router.get("/plan")
async def get_plan(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await plan_status(session, agent.organization_id)


@router.get("/plans")
async def public_plans(_: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Plan).where(Plan.is_public).order_by(Plan.position, Plan.id))).all()
    return [{"key": p.key, "name": p.name, "description": p.description, "price_month_usd": float(p.price_month_usd),
             "limits": p.limits, "features": p.features, "purchasable": bool(p.provider_price_id)} for p in rows]


@router.post("/billing/checkout")
async def checkout(body: CheckoutIn, agent: Agent = Depends(require_admin),
                   session: AsyncSession = Depends(get_session)):
    plan = await session.scalar(select(Plan).where(Plan.key == body.plan_key, Plan.is_public))
    if not plan:
        raise HTTPException(404, "Plan no encontrado")
    if not plan.provider_price_id:
        raise HTTPException(422, "Este plan no tiene precio configurado en el proveedor de pagos; contacta a ventas")
    org = await session.get(Organization, agent.organization_id)
    sub = await session.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    p = provider()
    try:
        customer = await p.ensure_customer(sub.provider_customer_id if sub and sub.provider == p.name else None,
                                           org.billing_email or agent.email, org.name, org.id)
        if sub is None:
            sub = Subscription(organization_id=org.id, plan_id=plan.id, provider=p.name, status="incomplete")
            session.add(sub)
        sub.provider, sub.provider_customer_id = p.name, customer
        await session.commit()
        url = await p.checkout_url(customer, plan.provider_price_id, org.id, plan.key,
                                   f"{settings.frontend_base_url}/configuraciones/plan?checkout=ok",
                                   f"{settings.frontend_base_url}/configuraciones/plan?checkout=cancel")
    except BillingError as e:
        raise HTTPException(502, f"Proveedor de pagos: {e}") from None
    return {"url": url}


@router.post("/billing/portal")
async def portal(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    sub = await session.scalar(select(Subscription).where(Subscription.organization_id == agent.organization_id))
    if not sub or not sub.provider_customer_id:
        raise HTTPException(409, "Todavía no tienes una suscripción con medio de pago")
    try:
        url = await provider().portal_url(sub.provider_customer_id,
                                          f"{settings.frontend_base_url}/configuraciones/plan")
    except BillingError as e:
        raise HTTPException(502, f"Proveedor de pagos: {e}") from None
    return {"url": url}


@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """Webhook de Stripe: firma verificada, idempotente por id de evento."""
    raw = await request.body()
    p = provider()
    try:
        event = p.parse_webhook(raw, request.headers.get("Stripe-Signature"))
    except BillingError as e:
        raise HTTPException(400, str(e)) from None
    payload = json.loads(raw)
    async with SessionLocal() as session:
        try:
            result = await apply_event(session, p.name, event, payload)
        except IntegrityError:  # el mismo evento llegó dos veces en paralelo
            await session.rollback()
            result = "duplicate"
        org_id = None
        if result != "duplicate":
            org_id = await session.scalar(select(BillingEvent.organization_id).where(
                BillingEvent.provider == p.name, BillingEvent.provider_event_id == event.id))
            session.add(InboundEvent(organization_id=org_id, source="stripe", payload=payload, processed_at=utcnow()))
            await session.commit()
    log.info("Stripe %s %s → %s", event.type, event.id, result)
    return {"ok": True, "result": result}
