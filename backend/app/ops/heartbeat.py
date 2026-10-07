"""Latido de cada proceso en worker_heartbeats y verificación de /health/ready (docs/data-model.md §12.1)."""

import asyncio
import json
import logging
import os
import socket
import time

from sqlalchemy import text

from app.config import get_settings
from app.ops import metrics
from app.ops.leader import STATUS

log = logging.getLogger(__name__)

BEAT_EVERY = 15.0
STALE_AFTER = 60.0
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_last_beat = {"mono": None}


def _loops() -> dict:
    return {name: {k: v for k, v in st.items() if k != "last_ok_mono"} for name, st in STATUS.items()}


async def beat() -> None:
    from app.db import engine

    s = get_settings()
    async with engine.begin() as conn:
        await conn.execute(text("""
            insert into public.worker_heartbeats (worker_id, role, hostname, version, loops)
            values (:w, :r, :h, :v, cast(:l as jsonb))
            on conflict (worker_id) do update set role = excluded.role, version = excluded.version,
              loops = excluded.loops, last_beat_at = now()"""),
            {"w": WORKER_ID, "r": s.role, "h": socket.gethostname(), "v": s.app_version,
             "l": json.dumps(_loops(), default=str)})
    _last_beat["mono"] = time.monotonic()


async def heartbeat_loop() -> None:
    while True:
        try:
            await beat()
        except Exception:
            log.warning("No se pudo registrar el latido del proceso", exc_info=True)
        await asyncio.sleep(BEAT_EVERY)


async def remove() -> None:
    from app.db import engine

    try:
        async with engine.begin() as conn:
            await conn.execute(text("delete from public.worker_heartbeats where worker_id = :w"), {"w": WORKER_ID})
    except Exception:
        log.debug("No se pudo borrar el latido", exc_info=True)


def beat_age() -> float | None:
    return None if _last_beat["mono"] is None else time.monotonic() - _last_beat["mono"]


async def readiness() -> tuple[bool, dict]:
    """DB alcanzable; en pg, LISTEN conectado (api); en worker, latido reciente."""
    from app.db import engine
    from app.realtime import hub

    s = get_settings()
    checks: dict[str, dict] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
        checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    if s.role in ("api", "all") and hub.mode == "pg":
        checks["realtime"] = {"ok": hub.listening, "mode": "pg"}
    if s.role in ("worker", "all"):
        age = beat_age()
        checks["heartbeat"] = {"ok": age is not None and age < STALE_AFTER,
                               "age_s": None if age is None else round(age, 1),
                               "leader_of": sorted(n for n, st in STATUS.items() if st.get("leader"))}
    return all(c["ok"] for c in checks.values()), {"role": s.role, "version": s.app_version, "worker_id": WORKER_ID,
                                                    "checks": checks}


metrics.Gauge("wa_heartbeat_age_seconds", "Segundos desde el último latido registrado",
              fn=lambda: beat_age() if beat_age() is not None else -1)
