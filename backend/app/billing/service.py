"""Reglas de facturación: aplica los eventos del proveedor a subscriptions y organizations (idempotente)."""

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.base import WebhookEvent
from app.models import Alert, BillingEvent, Organization, Plan, Subscription, utcnow

# Estado del proveedor → estado de la empresa
ORG_STATUS = {"trialing": "trial", "active": "active", "past_due": "past_due", "unpaid": "past_due",
              "canceled": "cancelled", "incomplete": None, "incomplete_expired": "cancelled", "paused": "past_due"}
SUB_STATUSES = ("trialing", "active", "past_due", "canceled", "incomplete", "unpaid")


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


async def _plan_for(session: AsyncSession, obj: dict) -> Plan | None:
    key = (obj.get("metadata") or {}).get("plan_key")
    if key:
        plan = await session.scalar(select(Plan).where(Plan.key == key))
        if plan:
            return plan
    items = ((obj.get("items") or {}).get("data") or [])
    price_id = ((items[0].get("price") or {}).get("id")) if items else None
    if price_id:
        return await session.scalar(select(Plan).where(Plan.provider_price_id == price_id))
    return None


async def _org_for(session: AsyncSession, obj: dict) -> Organization | None:
    meta = obj.get("metadata") or {}
    raw = meta.get("org_id") or obj.get("client_reference_id")
    if raw and str(raw).isdigit():
        org = await session.get(Organization, int(raw))
        if org:
            return org
    sub_id = obj.get("subscription") if isinstance(obj.get("subscription"), str) else None
    ids = [x for x in (obj.get("customer"), sub_id, obj.get("id") if obj.get("object") == "subscription" else None) if x]
    if not ids:
        return None
    sub = await session.scalar(select(Subscription).where(or_(
        Subscription.provider_customer_id.in_(ids), Subscription.provider_subscription_id.in_(ids))))
    return await session.get(Organization, sub.organization_id) if sub else None


async def _subscription(session: AsyncSession, org: Organization, plan_id: int | None) -> Subscription:
    sub = await session.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    if not sub:
        sub = Subscription(organization_id=org.id, plan_id=plan_id or org.plan_id, provider="stripe", status="incomplete")
        session.add(sub)
    return sub


def _alert(session: AsyncSession, org: Organization, title: str, description: str, severity: str = "warning") -> None:
    session.add(Alert(organization_id=org.id, severity=severity, layer="billing", source="system", title=title,
                      description=description))


async def apply_event(session: AsyncSession, provider: str, event: WebhookEvent, raw: dict) -> str:
    """Registra y aplica el evento. Devuelve 'duplicate', 'ignored' o 'processed'."""
    if await session.scalar(select(BillingEvent.id).where(BillingEvent.provider == provider,
                                                          BillingEvent.provider_event_id == event.id)):
        return "duplicate"
    obj = event.data
    org = await _org_for(session, obj)
    record = BillingEvent(provider=provider, provider_event_id=event.id, type=event.type,
                          organization_id=org.id if org else None, payload=raw)
    session.add(record)
    if not org:
        record.error = "No se pudo identificar la empresa"
        record.processed_at = utcnow()
        await session.commit()
        return "ignored"

    t = event.type
    if t == "checkout.session.completed":
        plan = await _plan_for(session, obj)
        sub = await _subscription(session, org, plan.id if plan else None)
        sub.provider = provider
        sub.provider_customer_id = obj.get("customer") or sub.provider_customer_id
        sub.provider_subscription_id = obj.get("subscription") or sub.provider_subscription_id
        if plan:
            sub.plan_id, org.plan_id = plan.id, plan.id
        sub.status = "active"
        org.status, org.trial_ends_at = "active", None
    elif t in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        plan = await _plan_for(session, obj)
        sub = await _subscription(session, org, plan.id if plan else None)
        status = "canceled" if t.endswith("deleted") else obj.get("status", "active")
        sub.provider = provider
        sub.status = status if status in SUB_STATUSES else "incomplete"
        sub.provider_customer_id = obj.get("customer") or sub.provider_customer_id
        sub.provider_subscription_id = obj.get("id") or sub.provider_subscription_id
        item = (((obj.get("items") or {}).get("data") or [{}])[0])
        sub.current_period_start = _ts(obj.get("current_period_start") or item.get("current_period_start"))
        sub.current_period_end = _ts(obj.get("current_period_end") or item.get("current_period_end"))
        sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        if plan:
            sub.plan_id, org.plan_id = plan.id, plan.id
        new_status = ORG_STATUS.get(status)
        if new_status and org.status != "suspended":  # la suspensión manual de la plataforma prevalece
            org.status = new_status
            if new_status == "active":
                org.trial_ends_at = None
        if new_status == "cancelled":
            _alert(session, org, "Tu suscripción fue cancelada", "Reactívala en Plan y facturación.", "critical")
    elif t == "invoice.paid":
        if org.status == "past_due":
            org.status = "active"
    elif t == "invoice.payment_failed":
        if org.status not in ("suspended", "cancelled"):
            org.status = "past_due"
        _alert(session, org, "No pudimos cobrar tu suscripción",
               "Actualiza el medio de pago en Plan y facturación para evitar la suspensión.", "critical")
    else:
        record.processed_at = utcnow()
        await session.commit()
        return "ignored"
    record.processed_at = utcnow()
    await session.commit()
    return "processed"
