"""Logs (texto o JSON con request id y empresa), Sentry opcional y middleware de métricas HTTP."""

import contextvars
import json
import logging
import time
import uuid

from app.config import get_settings
from app.ops import metrics

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
org_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("org_id", default=None)


def bind_org(org_id: int | None) -> None:
    """Otros módulos pueden fijar la empresa del contexto actual para los logs."""
    org_id_var.set(org_id)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.org_id = org_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage(),
               "request_id": getattr(record, "request_id", None), "org_id": getattr(record, "org_id", None)}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in out.items() if v is not None}, default=str, ensure_ascii=False)


def setup_logging() -> None:
    s = get_settings()
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if s.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    if s.log_format == "json":  # uvicorn trae sus propios handlers de texto: que pasen por el raíz
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            lg.handlers = []
            lg.propagate = True


def setup_sentry() -> None:
    s = get_settings()
    if not s.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN definido pero sentry-sdk no está instalado")
        return
    sentry_sdk.init(dsn=s.sentry_dsn, release=s.app_version, traces_sample_rate=0.0, send_default_pii=False)


def _org_from_auth(headers: dict[bytes, bytes]) -> int | None:
    """Empresa del token del panel (solo para los logs; si el token no es válido no se anota nada)."""
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from app.auth import decode_token

        org = decode_token(auth[7:]).get("org")
        return int(org) if org is not None else None
    except Exception:
        return None


class ObservabilityMiddleware:
    """ASGI puro: request id (X-Request-ID), empresa en los logs y métricas por ruta plantilla."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("latin-1")[:64] or uuid.uuid4().hex[:16]
        t_rid = request_id_var.set(rid)
        t_org = org_id_var.set(_org_from_auth(headers))
        status = {"code": 500}
        start = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "")
            metrics.http_requests.inc(method, path, str(status["code"]))
            metrics.http_latency.observe(time.perf_counter() - start, method, path)
            request_id_var.reset(t_rid)
            org_id_var.reset(t_org)
