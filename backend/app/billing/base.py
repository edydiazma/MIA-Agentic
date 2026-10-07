"""Interfaz de proveedor de pagos. Cambiar Stripe por Mercado Pago/Wompi = otro adaptador con estos métodos."""

from dataclasses import dataclass
from typing import Protocol


class BillingError(Exception):
    pass


@dataclass
class WebhookEvent:
    id: str
    type: str
    data: dict  # objeto principal del evento


class BillingProvider(Protocol):
    name: str

    async def ensure_customer(self, customer_id: str | None, email: str | None, name: str, org_id: int) -> str: ...

    async def checkout_url(self, customer_id: str, price_id: str, org_id: int, plan_key: str,
                           success_url: str, cancel_url: str) -> str: ...

    async def portal_url(self, customer_id: str, return_url: str) -> str: ...

    def parse_webhook(self, raw_body: bytes, signature_header: str | None) -> WebhookEvent: ...
