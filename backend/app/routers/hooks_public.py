"""Endpoint público de los webhooks entrantes: POST /hooks/{slug} (token en X-Hook-Token o ?token=)."""

import hmac
import json
import logging
import time
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app import inbound_hooks as hooks
from app.db import SessionLocal
from app.models import InboundWebhook

router = APIRouter(prefix="/hooks", tags=["inbound-webhooks-public"])
log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


def _error(status: int, code: str, message: str, details: list | None = None, **extra) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


async def _payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise hooks.HookError(413, "too_large", "El cuerpo supera 64 KB")
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = await request.form()
        return {k: v for k, v in form.items() if isinstance(v, str)}
    if not raw.strip():
        return dict(request.query_params)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise hooks.HookError(400, "invalid_json", "El cuerpo no es JSON válido") from e
    if not isinstance(data, dict):
        raise hooks.HookError(400, "invalid_json", "El cuerpo debe ser un objeto JSON")
    return data


@router.post("/{slug}")
async def trigger(slug: str, request: Request):
    started = time.monotonic()
    async with SessionLocal() as session:
        hook = await session.scalar(select(InboundWebhook).where(InboundWebhook.slug == slug))
        if hook is None:
            return _error(404, "not_found", "Webhook no encontrado")
        token = request.headers.get("x-hook-token") or request.query_params.get("token") or ""
        if not token or not hmac.compare_digest(hooks.hash_token(token), hook.token_hash):
            await hooks.record_run(session, hook, "rejected", 401, None, None, "Token inválido", started, _ip(request),
                                   None)
            return _error(401, "unauthorized", "Token inválido")
        if hook.status != "active":
            await hooks.record_run(session, hook, "rejected", 409, None, None, f"Webhook {hook.status}", started,
                                   _ip(request), None)
            return _error(409, "inactive", "El webhook no está activo (publícalo o actívalo en el panel)")
        if await hooks.rate_limited(session, hook):
            return _error(429, "rate_limited", "Demasiadas llamadas: intenta en un minuto",
                          retry_after=60)
        payload: dict | None = None
        idem = request.headers.get("idempotency-key")
        try:
            payload = await _payload(request)
            payload.pop("token", None)
            parsed = hooks.parse_params(hook, payload)
            dedupe_minutes = float((hook.options or {}).get("dedupe_minutes") or 0)
            key = f"key:{idem[:200]}" if idem else (hooks.dedupe_key(hook, parsed) if dedupe_minutes else None)
            window = timedelta(hours=24) if idem else timedelta(minutes=dedupe_minutes)
            if key:
                previous = await hooks.find_duplicate(session, hook, key, window)
                if previous:
                    run_id = await hooks.record_run(session, hook, "duplicate", 200, payload,
                                                    {"duplicate_of": previous}, None, started, _ip(request), key)
                    return JSONResponse({"ok": True, "duplicate": True, "run_id": run_id, "duplicate_of": previous})
            result = await hooks.execute(session, hook, payload)
        except hooks.HookError as e:
            await session.rollback()
            await session.refresh(hook)
            status = "rejected" if e.status in (400, 413, 422) else "failed"
            await hooks.record_run(session, hook, status, e.status, payload, {"details": e.details} if e.details else None,
                                   e.message, started, _ip(request), idem and f"key:{idem[:200]}")
            return _error(e.status, e.code, e.message, e.details)
        except Exception as e:  # noqa: BLE001 — el sistema externo recibe un error claro y queda registro
            log.exception("Webhook %s falló", slug)
            await session.rollback()
            await session.refresh(hook)
            await hooks.record_run(session, hook, "failed", 500, payload, None, f"{type(e).__name__}: {e}", started,
                                   _ip(request), idem and f"key:{idem[:200]}")
            return _error(500, "internal_error", "Error interno al procesar el webhook")
        run_id = await hooks.record_run(session, hook, "succeeded", 200, payload, result, None, started, _ip(request),
                                        key)
        return JSONResponse({"ok": True, "run_id": run_id, "contact_id": result.get("contact_id"),
                             "message_id": result.get("message_id"), **{k: v for k, v in result.items()
                                                                       if k.endswith("_id") and v is not None}})
