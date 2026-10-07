"""Base de la API pública /v1: errores, autenticación por llave, límites, registro, idempotencia y paginación.

- Errores: siempre {"error": {"code", "message"}}.
- Autenticación: Authorization: Bearer wak_live_... (o X-API-Key). Llave revocada/vencida → 401; plan sin «api»
  → 402; empresa suspendida → 403; alcance faltante → 403.
- Límite por llave (rate_limit_per_min) compartido entre réplicas: public.rate_limit_hit() → 429 con Retry-After
  y X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset en todas las respuestas.
- Registro: cada llamada autenticada va a api_requests (ruta plantilla, estado, latencia) en lotes, sin
  bloquear la respuesta.
- Idempotency-Key en POST: misma llave y mismo cuerpo → misma respuesta; cuerpo distinto → 409 (24 h).
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_session
from app.models import ApiIdempotency, ApiKey, ApiRequest, Organization, utcnow
from app.public_api.keys import hash_key, parse

log = logging.getLogger(__name__)
WINDOW_S = 60
CODES = {400: "bad_request", 401: "unauthorized", 402: "payment_required", 403: "forbidden", 404: "not_found",
         405: "method_not_allowed", 409: "conflict", 422: "validation_error", 429: "rate_limited",
         500: "internal_error", 502: "upstream_error", 503: "unavailable"}


class ApiError(HTTPException):
    def __init__(self, status: int, message: str, code: str | None = None, headers: dict | None = None):
        super().__init__(status, message, headers=headers)
        self.code = code or CODES.get(status, "error")


def error_body(status: int, message, code: str | None = None) -> dict:
    return {"error": {"code": code or CODES.get(status, "error"), "message": message}}


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = getattr(exc, "code", None)
    message = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
    resp = JSONResponse(error_body(exc.status_code, message, code), status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))
    _rate_headers(request, resp)
    return resp


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    parts = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query", "path"))
        parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
    resp = JSONResponse(error_body(422, "; ".join(parts) or "Datos inválidos"), status_code=422)
    _rate_headers(request, resp)
    return resp


# --- Autenticación ----------------------------------------------------------------
@dataclass
class ApiContext:
    key: ApiKey
    org: int
    scopes: set[str]


def _raw_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


async def api_context(request: Request, session: AsyncSession = Depends(get_session)) -> ApiContext:
    from app.plans import has_feature
    from app.tenancy import org_access_error

    raw = _raw_key(request)
    if not raw or not parse(raw):
        raise ApiError(401, "Falta la llave de API o su formato no es válido (Authorization: Bearer wak_live_…)")
    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    if not key or key.revoked_at:
        raise ApiError(401, "Llave de API inválida o revocada")
    if key.expires_at and key.expires_at <= utcnow():
        raise ApiError(401, "La llave de API venció", code="key_expired")
    request.state.api_key_id, request.state.org = key.id, key.organization_id
    error = org_access_error(await session.get(Organization, key.organization_id))
    if error:
        raise ApiError(error[0], error[1])
    if not await has_feature(session, key.organization_id, "api"):
        raise ApiError(402, "El plan de la empresa no incluye la API")
    await _rate_limit(request, session, key)
    now = utcnow()
    if not key.last_used_at or (now - key.last_used_at).total_seconds() > 60:  # sin escribir en cada llamada
        key.last_used_at, key.last_used_ip = now, _client_ip(request)
        await session.commit()
    return ApiContext(key=key, org=key.organization_id, scopes=set(key.scopes or []))


def require(scope: str):
    async def dependency(ctx: ApiContext = Depends(api_context)) -> ApiContext:
        if scope not in ctx.scopes:
            raise ApiError(403, f"La llave no tiene el alcance «{scope}»", code="missing_scope")
        return ctx
    return dependency


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


async def _rate_limit(request: Request, session: AsyncSession, key: ApiKey) -> None:
    hits = int(await session.scalar(text("select public.rate_limit_hit(:b, :w)"),
                                    {"b": f"api_key:{key.id}", "w": WINDOW_S}))
    await session.commit()
    limit = key.rate_limit_per_min
    reset = WINDOW_S - int(time.time()) % WINDOW_S
    request.state.rate = (limit, max(0, limit - hits), reset)
    if hits > limit:
        raise ApiError(429, f"Límite de {limit} solicitudes por minuto superado", headers={"Retry-After": str(reset)})


def _rate_headers(request: Request, resp) -> None:
    rate = getattr(request.state, "rate", None)
    if rate:
        resp.headers["X-RateLimit-Limit"] = str(rate[0])
        resp.headers["X-RateLimit-Remaining"] = str(rate[1])
        resp.headers["X-RateLimit-Reset"] = str(rate[2])


# --- Registro de solicitudes (en lotes) --------------------------------------------
_buffer: list[dict] = []
_flusher: asyncio.Task | None = None
FLUSH_EVERY_S = 2.0
FLUSH_SIZE = 100


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not path:  # sin ruta (404): se reemplazan los segmentos numéricos
        path = "/".join("{id}" if p.isdigit() else p for p in request.url.path.split("/"))
        return path[:200]
    return ("/v1" + path)[:200]


def enqueue_log(request: Request, status: int, started: float) -> None:
    org = getattr(request.state, "org", None)
    if org is None:
        return
    error = None
    if status >= 400:
        error = CODES.get(status, "error")
    _buffer.append({"organization_id": org, "api_key_id": getattr(request.state, "api_key_id", None),
                    "method": request.method, "path": _route_template(request), "status": status,
                    "latency_ms": int((time.monotonic() - started) * 1000), "ip": _client_ip(request),
                    "error_code": error, "created_at": utcnow()})
    global _flusher
    if _flusher is None or _flusher.done():
        try:
            _flusher = asyncio.get_running_loop().create_task(_flush_loop())
        except RuntimeError:
            pass


async def flush_logs() -> int:
    if not _buffer:
        return 0
    rows = _buffer[:]
    del _buffer[:len(rows)]
    try:
        async with SessionLocal() as session:
            await session.execute(insert(ApiRequest), rows)
            await session.commit()
    except Exception:
        log.exception("No se pudo guardar el registro de la API (%s filas)", len(rows))
    return len(rows)


async def _flush_loop() -> None:
    while _buffer:
        await asyncio.sleep(0 if len(_buffer) >= FLUSH_SIZE else FLUSH_EVERY_S)
        await flush_logs()


async def logging_middleware(request: Request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        enqueue_log(request, 500, started)
        raise
    _rate_headers(request, response)
    enqueue_log(request, response.status_code, started)
    return response


# --- Idempotencia ------------------------------------------------------------------
class Idempotency:
    """Uso: `if idem.replay: return idem.replay` ... `return await idem.respond(payload, 201)`."""

    def __init__(self, session: AsyncSession, org: int, key: str | None, body_hash: str):
        self.session, self.org, self.key, self.body_hash = session, org, key, body_hash
        self.replay: JSONResponse | None = None
        self.ctx: ApiContext | None = None

    async def claim(self) -> None:
        if not self.key:
            return
        if len(self.key) > 200:
            raise ApiError(422, "Idempotency-Key demasiado larga (máx. 200)")
        row = await self.session.get(ApiIdempotency, (self.org, self.key))
        if row is None:
            try:
                await self.session.execute(insert(ApiIdempotency).values(
                    organization_id=self.org, key=self.key, request_hash=self.body_hash))
                await self.session.commit()
                return
            except IntegrityError:
                await self.session.rollback()
                row = await self.session.get(ApiIdempotency, (self.org, self.key))
        if row.request_hash != self.body_hash:
            raise ApiError(409, "Idempotency-Key ya se usó con otro cuerpo", code="idempotency_conflict")
        if row.status is None:
            raise ApiError(409, "Hay una solicitud con esta Idempotency-Key en curso", code="idempotency_in_progress")
        self.replay = JSONResponse(row.response, status_code=row.status, headers={"Idempotent-Replayed": "true"})

    async def respond(self, payload, status: int = 200) -> JSONResponse:
        body = json.loads(json.dumps(payload, default=str))
        if self.key:
            row = await self.session.get(ApiIdempotency, (self.org, self.key))
            if row is not None:
                row.status, row.response = status, body
                await self.session.commit()
        return JSONResponse(body, status_code=status)

    async def release(self) -> None:
        """La operación falló: se libera la llave para reintentar."""
        if self.key:
            await self.session.rollback()
            row = await self.session.get(ApiIdempotency, (self.org, self.key))
            if row is not None and row.status is None:
                await self.session.delete(row)
                await self.session.commit()


def idempotency(scope: str):
    async def dependency(request: Request, ctx: ApiContext = Depends(require(scope)),
                         session: AsyncSession = Depends(get_session)) -> Idempotency:
        body = await request.body()
        idem = Idempotency(session, ctx.org, request.headers.get("idempotency-key"),
                           hashlib.sha256(request.method.encode() + request.url.path.encode() + body).hexdigest())
        idem.ctx = ctx
        await idem.claim()
        try:
            yield idem
        except Exception:
            await idem.release()  # la operación falló: se puede reintentar con la misma llave
            raise
    return dependency


# --- Paginación por cursor ----------------------------------------------------------
def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": last_id}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        return int(json.loads(base64.urlsafe_b64decode(cursor + pad))["id"])
    except (ValueError, KeyError, TypeError) as e:
        raise ApiError(400, "Cursor inválido") from e


def page(items: list, limit: int, last_id) -> dict:
    """items trae limit+1 filas: la sobrante indica que hay otra página."""
    more = len(items) > limit
    data = items[:limit]
    return {"data": data, "next_cursor": encode_cursor(last_id(data[-1])) if more and data else None}
