"""Adaptador de Stripe por API REST (form-encoded) con httpx; sin SDK."""

import hashlib
import hmac
import json
import time

import httpx

from app.billing.base import BillingError, WebhookEvent
from app.config import get_settings

API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_S = 300


def flatten(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """{"a": {"b": 1}, "c": [{"d": 2}]} → [("a[b]", "1"), ("c[0][d]", "2")] (formato de Stripe)."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out += flatten(value, name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                out += flatten(item, f"{name}[{i}]") if isinstance(item, dict) else [(f"{name}[{i}]", str(item))]
        elif value is not None:
            out.append((name, "true" if value is True else "false" if value is False else str(value)))
    return out


def verify_signature(raw_body: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET no está configurado")
    if not header:
        raise BillingError("Falta la cabecera Stripe-Signature")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        raise BillingError("Firma de Stripe mal formada") from None
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        raise BillingError("Firma de Stripe vencida")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError("Firma de Stripe inválida")


class StripeProvider:
    name = "stripe"

    def __init__(self, secret_key: str | None = None, webhook_secret: str | None = None):
        s = get_settings()
        self.secret_key = secret_key or s.stripe_secret_key
        self.webhook_secret = webhook_secret or s.stripe_webhook_secret

    async def _post(self, path: str, data: dict) -> dict:
        if not self.secret_key:
            raise BillingError("STRIPE_SECRET_KEY no está configurado")
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{API}{path}", data=flatten(data), auth=(self.secret_key, ""))
        body = r.json()
        if r.status_code >= 400:
            raise BillingError((body.get("error") or {}).get("message") or f"Stripe {r.status_code}")
        return body

    async def ensure_customer(self, customer_id: str | None, email: str | None, name: str, org_id: int) -> str:
        if customer_id:
            return customer_id
        c = await self._post("/customers", {"email": email, "name": name, "metadata": {"org_id": org_id}})
        return c["id"]

    async def checkout_url(self, customer_id: str, price_id: str, org_id: int, plan_key: str,
                           success_url: str, cancel_url: str) -> str:
        session = await self._post("/checkout/sessions", {
            "mode": "subscription", "customer": customer_id, "client_reference_id": org_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url, "cancel_url": cancel_url, "allow_promotion_codes": True,
            "metadata": {"org_id": org_id, "plan_key": plan_key},
            "subscription_data": {"metadata": {"org_id": org_id, "plan_key": plan_key}},
        })
        return session["url"]

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        portal = await self._post("/billing_portal/sessions", {"customer": customer_id, "return_url": return_url})
        return portal["url"]

    def parse_webhook(self, raw_body: bytes, signature_header: str | None) -> WebhookEvent:
        verify_signature(raw_body, signature_header, self.webhook_secret)
        event = json.loads(raw_body)
        return WebhookEvent(id=event["id"], type=event["type"], data=(event.get("data") or {}).get("object") or {})
