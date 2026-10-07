"""Entrega de eventos a webhooks salientes (firmados con HMAC-SHA256; secreto en Vault).

Cada intento queda en webhook_deliveries. Tras 10 fallas seguidas el webhook se desactiva y se crea una alerta.
"""

import hashlib
import hmac
import json
import logging
import time

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Alert, OutboundWebhook, WebhookDelivery, utcnow
from app.secrets_vault import get_secret

log = logging.getLogger(__name__)
DISABLE_AFTER = 10
_cache: tuple[float, list[tuple[int, int, str, str, list]]] = (0.0, [])


def invalidate_cache() -> None:
    global _cache
    _cache = (0.0, [])


async def _targets() -> list[tuple[int, int, str, str, list]]:
    """(id, org, url, secreto, eventos) de los webhooks activos (caché de 30 s)."""
    global _cache
    if time.time() - _cache[0] < 30:
        return _cache[1]
    async with SessionLocal() as session:
        rows = (await session.scalars(select(OutboundWebhook).where(OutboundWebhook.active))).all()
        out = [(w.id, w.organization_id, w.url, await get_secret(session, w.signing_secret_id) or "", w.events or [])
               for w in rows]
    _cache = (time.time(), out)
    return out


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(event: str, data: dict, only_id: int | None = None, organization_id: int | None = None) -> None:
    """Entrega el evento SOLO a los webhooks de su organización (o al webhook `only_id` en una prueba)."""
    for wid, worg, url, secret, events in await _targets():
        if only_id is not None:
            if wid != only_id:
                continue
        elif organization_id is None or worg != organization_id or (events and event not in events):
            continue
        body = json.dumps({"event": event, "data": data, "sent_at": utcnow().isoformat()}, default=str).encode()
        status, error = None, None
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(url, content=body,
                                    headers={"Content-Type": "application/json", "X-Signature-256": sign(secret, body)})
            status = r.status_code
            if r.status_code >= 400:
                error = r.text[:500]
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:500]
        await _record(wid, event, status, error, int((time.monotonic() - started) * 1000))


async def _record(wid: int, event: str, status: int | None, error: str | None, latency_ms: int) -> None:
    async with SessionLocal() as session:
        w = await session.get(OutboundWebhook, wid)
        if not w:
            return
        session.add(WebhookDelivery(webhook_id=wid, event=event, status_code=status, latency_ms=latency_ms, error=error))
        w.last_status, w.last_error, w.last_delivery_at = status, error, utcnow()
        w.consecutive_failures = w.consecutive_failures + 1 if error else 0
        if w.consecutive_failures >= DISABLE_AFTER and w.active:
            w.active = False
            session.add(Alert(
                organization_id=w.organization_id, severity="critical", layer="webhook", source="system",
                title=f"Webhook «{w.name}» desactivado",
                description=f"{DISABLE_AFTER} entregas fallidas seguidas. Último error: {error}", ref=str(w.id),
            ))
            invalidate_cache()
        await session.commit()
