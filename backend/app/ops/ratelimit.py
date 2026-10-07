"""Límites de uso compartidos entre réplicas: public.rate_limit_hit(bucket, window_s) (ventanas fijas).

El contador se escribe en su propia transacción (no depende del commit del llamador) y, si la base falla,
se deja pasar la petición (fail-open): un límite nunca debe tumbar el servicio.
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.ops import metrics

log = logging.getLogger(__name__)


async def hits(session_or_conn: AsyncSession | AsyncConnection | AsyncEngine | None, bucket: str,
               window_s: int) -> int:
    sql = text("select public.rate_limit_hit(:b, :w)")
    params = {"b": bucket[:200], "w": int(window_s)}
    if isinstance(session_or_conn, AsyncConnection):
        return int(await session_or_conn.scalar(sql, params))
    if isinstance(session_or_conn, AsyncSession):
        engine = session_or_conn.bind
    elif isinstance(session_or_conn, AsyncEngine):
        engine = session_or_conn
    else:
        from app.db import engine
    async with engine.begin() as conn:
        return int(await conn.scalar(sql, params))


async def allow(session_or_conn, bucket: str, limit: int, window_s: int) -> bool:
    try:
        return await hits(session_or_conn, bucket, window_s) <= limit
    except Exception:
        log.warning("Límite de uso no disponible para %s: se deja pasar", bucket, exc_info=True)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce(session_or_conn, bucket: str, limit: int, window_s: int, scope: str) -> None:
    """Lanza 429 (JSON con Retry-After) si se pasó el límite."""
    if not await allow(session_or_conn, bucket, limit, window_s):
        metrics.rate_limited.inc(scope)
        raise HTTPException(429, "Demasiadas solicitudes; intenta de nuevo en un momento",
                            headers={"Retry-After": str(window_s)})


def per_ip(scope: str, limit: int, window_s: int = 60):
    """Dependencia de FastAPI: límite por IP para una ruta pública."""
    async def dependency(request: Request) -> None:
        await enforce(None, f"ip:{client_ip(request)}:{scope}", limit, window_s, scope)
    return dependency


async def peek(bucket: str, window_s: int) -> int:
    """Golpes en la ventana actual sin sumar uno (p. ej. intentos fallidos acumulados)."""
    from app.db import engine

    try:
        async with engine.connect() as conn:
            return int(await conn.scalar(text(
                "select coalesce((select hits from public.rate_limit_counters where bucket = :b and window_start = "
                "to_timestamp(floor(extract(epoch from now()) / :w) * :w)), 0)"), {"b": bucket[:200], "w": int(window_s)}))
    except Exception:
        log.warning("Límite de uso no disponible para %s", bucket, exc_info=True)
        return 0
