"""Cola de trabajos en Postgres (docs/data-model.md §19.2).

- `enqueue(session, kind, payload, ...)` agrega un trabajo en la transacción del llamador (se ve al hacer commit:
  si la transacción se revierte, el trabajo tampoco existe). Con `dedupe_key` no se encola dos veces el mismo
  trabajo mientras esté pendiente o corriendo (índice único parcial) → devuelve None.
- `@job("kind", queue=...)` registra el manejador: `async def handler(payload: dict) -> dict | None`.
- Los procesos worker/all corren un consumidor por cola (`run_workers`): toman trabajos con
  `public.jobs_claim(...)` (FOR UPDATE SKIP LOCKED: varios workers sin pisarse), renuevan el lease mientras el
  manejador trabaja, reintentan con espera exponencial y pasan a "dead" (con alerta) al agotar los intentos.
- `run_pending()` ejecuta en línea lo que haya (pruebas y scripts).
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ops import metrics

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[dict | None]]
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 3600
POLL_IDLE_S = 1.0
STATS_EVERY_S = 15.0
ALERT_LAYERS = {"crm": "integration", "conversions": "conversion", "ads": "integration", "golden": "ai",
                "voice": "voice", "outbound": "campaign", "copilot": "ai", "email": "integration"}


@dataclass
class JobSpec:
    kind: str
    fn: Handler
    queue: str
    max_attempts: int


REGISTRY: dict[str, JobSpec] = {}

jobs_done = metrics.Counter("wa_jobs_total", "Trabajos terminados por cola, tipo y resultado", ("queue", "kind", "result"))
jobs_latency = metrics.Histogram("wa_job_duration_seconds", "Duración de los trabajos", ("queue",),
                                 buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300))
_stats: dict = {"depth": {}, "oldest": {}, "dead": {}}
metrics.Gauge("wa_jobs_queue_depth", "Trabajos listos o programados por cola", ("queue",),
              fn=lambda: {(q,): v for q, v in _stats["depth"].items()})
metrics.Gauge("wa_jobs_oldest_ready_seconds", "Antigüedad del trabajo listo más viejo por cola", ("queue",),
              fn=lambda: {(q,): v for q, v in _stats["oldest"].items()})
metrics.Gauge("wa_jobs_dead", "Trabajos muertos (últimos 30 días) por cola", ("queue",),
              fn=lambda: {(q,): v for q, v in _stats["dead"].items()})


def job(kind: str, queue: str = "default", max_attempts: int = 5):
    """Registra un manejador. El tipo es único en todo el proceso."""
    def deco(fn: Handler) -> Handler:
        if kind in REGISTRY and REGISTRY[kind].fn is not fn:
            raise ValueError(f"Trabajo duplicado: {kind}")
        REGISTRY[kind] = JobSpec(kind, fn, queue, max_attempts)
        return fn
    return deco


def load_handlers() -> None:
    """Importa los módulos que registran manejadores (idempotente)."""
    import importlib

    for module in ("app.job_handlers", "app.journeys.scheduler"):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as e:
            if e.name != module:
                raise


_handlers_loaded = False


def _ensure_handlers() -> None:
    """La cola y los reintentos de cada tipo vienen del registro (@job en app.job_handlers): se carga una vez aquí
    para que un proceso que solo encola (API) no mande trabajos a la cola equivocada."""
    global _handlers_loaded
    if not _handlers_loaded:
        _handlers_loaded = True
        import app.job_handlers  # noqa: F401  (registra los manejadores)


async def enqueue(session: AsyncSession, kind: str, payload: dict, *, queue: str = "default",
                  organization_id: int | None = None, dedupe_key: str | None = None,
                  run_at: datetime | None = None, priority: int = 100) -> int | None:
    """Encola en la transacción del llamador (haz commit). None si ya había uno igual pendiente (dedupe_key)."""
    _ensure_handlers()
    spec = REGISTRY.get(kind)
    if queue == "default" and spec is not None:
        queue = spec.queue
    row = await session.execute(text("""
        insert into public.jobs (organization_id, queue, kind, payload, dedupe_key, priority, run_at, max_attempts)
        values (:org, :q, :k, cast(:p as jsonb), :d, :pr, coalesce(:at, now()), :ma)
        on conflict (queue, dedupe_key) where dedupe_key is not null and status in ('queued', 'running')
        do nothing
        returning id"""), {"org": organization_id, "q": queue, "k": kind, "p": json.dumps(payload, default=str),
                            "d": dedupe_key, "pr": priority, "at": run_at,
                            "ma": spec.max_attempts if spec else 5})
    found = row.first()
    return found[0] if found else None


async def schedule(kind: str, items: list[tuple[dict, str | None, int | None]], *, queue: str = "default") -> int:
    """Encola varios (payload, dedupe_key, organization_id) en una transacción propia; devuelve los nuevos.
    Lo usan los bucles programadores: con dedupe_key, un ítem que sigue pendiente no se duplica."""
    from app.db import SessionLocal

    if not items:
        return 0
    created = 0
    async with SessionLocal() as session:
        for payload, dedupe_key, org in items:
            if await enqueue(session, kind, payload, queue=queue, organization_id=org, dedupe_key=dedupe_key):
                created += 1
        await session.commit()
    return created


def enabled() -> bool:
    return get_settings().jobs_enabled


def backoff_seconds(attempts: int) -> float:
    """30 s, 60 s, 2 min, 4 min… hasta 1 h, con ±20 % de variación (evita que los reintentos lleguen juntos)."""
    base = min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_MAX_S)
    return base * random.uniform(0.8, 1.2)


async def _claim(queue: str, worker: str, limit: int, lease_s: int) -> list[dict]:
    from app.db import engine

    async with engine.begin() as conn:
        rows = (await conn.execute(text("select * from public.jobs_claim(:q, :w, :n, :l)"),
                                   {"q": queue, "w": worker, "n": limit, "l": lease_s})).mappings().all()
    return [dict(r) for r in rows]


async def _renew(job_id: int, worker: str, lease_s: int) -> None:
    from app.db import engine

    async with engine.begin() as conn:
        await conn.execute(text("""update public.jobs set locked_until = now() + make_interval(secs => :l)
                                   where id = :i and status = 'running' and locked_by = :w"""),
                           {"i": job_id, "w": worker, "l": lease_s})


async def _finish(job_row: dict, worker: str, ok: bool, result: dict | None, error: str | None) -> str:
    from app.db import engine

    attempts, max_attempts = job_row["attempts"], job_row["max_attempts"]
    if ok:
        status, run_at = "succeeded", None
    elif attempts >= max_attempts:
        status, run_at = "dead", None
    else:
        status, run_at = "queued", timedelta(seconds=backoff_seconds(attempts))
    async with engine.begin() as conn:
        await conn.execute(text("""
            update public.jobs set status = :s, result = cast(:r as jsonb), last_error = :e, locked_by = null,
                   locked_until = null,
                   run_at = case when :s = 'queued' then now() + make_interval(secs => :d) else run_at end,
                   finished_at = case when :s in ('succeeded', 'dead') then now() else null end
            where id = :i and locked_by = :w"""),
            {"s": status, "r": json.dumps(result, default=str) if result is not None else None,
             "e": error, "d": run_at.total_seconds() if run_at else 0, "i": job_row["id"], "w": worker})
    if status == "dead":
        await _alert_dead(job_row, error)
    return status


async def _alert_dead(job_row: dict, error: str | None) -> None:
    if not job_row.get("organization_id"):
        log.error("Trabajo %s (%s) agotó sus intentos: %s", job_row["id"], job_row["kind"], error)
        return
    try:
        from app.db import SessionLocal
        from app.service import create_alert

        layer = ALERT_LAYERS.get(job_row["kind"].split(".")[0], "integration")
        async with SessionLocal() as session:
            await create_alert(session, job_row["organization_id"], severity="warning", layer=layer,
                               source="system", title=f"Un proceso en segundo plano falló: {job_row['kind']}",
                               description=(error or "")[:1000], ref=f"job:{job_row['id']}")
    except Exception:
        log.exception("No se pudo crear la alerta del trabajo %s", job_row["id"])


async def execute(job_row: dict, worker: str, lease_s: int) -> str:
    """Corre un trabajo ya tomado: renueva el lease mientras trabaja y deja el resultado."""
    spec = REGISTRY.get(job_row["kind"])
    queue = job_row["queue"]
    started = time.monotonic()
    if spec is None:
        status = await _finish({**job_row, "attempts": job_row["max_attempts"]}, worker, False, None,
                               f"Sin manejador para {job_row['kind']}")
        jobs_done.inc(queue, job_row["kind"], status)
        return status

    async def keep_lease():
        while True:
            await asyncio.sleep(max(lease_s / 3, 1))
            try:
                await _renew(job_row["id"], worker, lease_s)
            except Exception:
                log.warning("No se pudo renovar el lease del trabajo %s", job_row["id"], exc_info=True)

    renewer = asyncio.create_task(keep_lease())
    try:
        payload = job_row["payload"] if isinstance(job_row["payload"], dict) else json.loads(job_row["payload"] or "{}")
        result = await spec.fn(payload)
        status = await _finish(job_row, worker, True, result if isinstance(result, dict) else None, None)
    except asyncio.CancelledError:
        # Apagado: el lease vence y otro worker lo retoma (no cuenta como fallo del manejador)
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Trabajo %s (%s) falló (intento %s/%s): %s", job_row["id"], job_row["kind"],
                    job_row["attempts"], job_row["max_attempts"], e)
        status = await _finish(job_row, worker, False, None, f"{type(e).__name__}: {e}"[:2000])
    finally:
        renewer.cancel()
    jobs_done.inc(queue, job_row["kind"], status)
    jobs_latency.observe(time.monotonic() - started, queue)
    return status


def concurrency() -> dict[str, int]:
    out: dict[str, int] = {}
    for part in get_settings().jobs_concurrency.split(","):
        name, _, n = part.strip().partition("=")
        if name:
            out[name] = max(1, int(n or 1))
    for spec in REGISTRY.values():  # una cola con manejadores registrados siempre tiene consumidor
        out.setdefault(spec.queue, 1)
    return out


async def consume(queue: str, slots: int, worker: str, stop: asyncio.Event) -> None:
    lease_s = get_settings().jobs_lease_seconds
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        free = slots - len(running)
        claimed = []
        if free > 0:
            try:
                claimed = await _claim(queue, worker, free, lease_s)
            except Exception:
                log.warning("No se pudieron tomar trabajos de la cola %s", queue, exc_info=True)
        for row in claimed:
            task = asyncio.create_task(execute(row, worker, lease_s))
            running.add(task)
            task.add_done_callback(running.discard)
        if not claimed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_IDLE_S)
            except TimeoutError:
                pass
    # Apagado ordenado: espera un poco a los que están corriendo; el resto lo retoma otro worker al vencer el lease
    if running:
        await asyncio.wait(running, timeout=20)
        for t in running:
            t.cancel()


async def refresh_stats() -> None:
    from app.db import engine

    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            select queue, count(*) filter (where status = 'queued') as depth,
                   coalesce(extract(epoch from now() - min(run_at) filter (where status = 'queued' and run_at <= now())), 0)
                     as oldest,
                   count(*) filter (where status = 'dead') as dead
            from public.jobs group by queue"""))).mappings().all()
    _stats["depth"] = {r["queue"]: r["depth"] for r in rows}
    _stats["oldest"] = {r["queue"]: float(r["oldest"]) for r in rows}
    _stats["dead"] = {r["queue"]: r["dead"] for r in rows}


async def run_workers() -> None:
    """Un consumidor por cola (worker/all). Se cancela al apagar el proceso."""
    from app.ops.heartbeat import WORKER_ID

    load_handlers()
    stop = asyncio.Event()
    queues = concurrency()
    consumers = [asyncio.create_task(consume(q, n, WORKER_ID, stop), name=f"jobs:{q}") for q, n in queues.items()]
    log.info("Cola de trabajos: %s", ", ".join(f"{q}×{n}" for q, n in queues.items()))
    try:
        while True:
            try:
                await refresh_stats()
                from app.db import engine

                async with engine.begin() as conn:
                    await conn.execute(text("select public.jobs_requeue_stuck()"))
            except Exception:
                log.debug("Estadísticas de la cola no disponibles", exc_info=True)
            await asyncio.sleep(STATS_EVERY_S)
    finally:
        stop.set()
        await asyncio.gather(*consumers, return_exceptions=True)


async def run_pending(queue: str | None = None, limit: int = 100, worker: str = "inline") -> int:
    """Ejecuta en este proceso los trabajos listos (pruebas, scripts, despliegues de un solo proceso)."""
    load_handlers()
    queues = [queue] if queue else sorted({s.queue for s in REGISTRY.values()} | set(concurrency()))
    done = 0
    for q in queues:
        while done < limit:
            rows = await _claim(q, worker, min(10, limit - done), get_settings().jobs_lease_seconds)
            if not rows:
                break
            for row in rows:
                await execute(row, worker, get_settings().jobs_lease_seconds)
                done += 1
    return done
