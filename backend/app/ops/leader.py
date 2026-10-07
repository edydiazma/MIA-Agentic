"""Tareas de fondo con un solo líder en el clúster (docs/data-model.md §12.1).

Cada tarea corre solo mientras su proceso tiene `pg_try_advisory_lock(hashtext('wa_loop:<nombre>'))`. Todos los
locks de un proceso viven en UNA conexión dedicada (LockSession): el Session pooler de Supabase limita las
conexiones por cliente. Si esa conexión se cae, Postgres libera los locks, el proceso pierde el liderazgo de
todas sus tareas (se cancelan) y otra réplica los toma en su siguiente intento (cada LOCK_RETRY segundos).
Si una tarea falla, se registra el error y se reinicia con espera creciente.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.ops import metrics

log = logging.getLogger(__name__)

LOCK_RETRY = 15.0
CHECK_EVERY = 15.0

# Estado por tarea para el latido y /metrics: {nombre: {leader, since, last_ok_at, last_error, last_error_at,
# restarts}}
STATUS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def report_ok(name: str) -> None:
    """Una tarea puede llamarla al terminar cada vuelta (mide el retraso en /metrics); es opcional."""
    STATUS.setdefault(name, {})["last_ok_at"] = _now()
    STATUS[name]["last_ok_mono"] = time.monotonic()


class LockSession:
    """Una conexión asyncpg compartida por todos los locks del proceso. `generation` cambia al reconectar:
    un líder que adquirió su lock en otra generación ya no lo tiene."""

    def __init__(self, connect=None):
        self._connect = connect
        self.conn = None
        self.generation = 0
        self._mutex = asyncio.Lock()

    async def _ensure(self):
        if self.conn is None or self.conn.is_closed():
            connect = self._connect
            if connect is None:
                from app.ops.pg import connect
            self.conn = await connect()
            self.generation += 1
        return self.conn

    async def try_lock(self, key: str) -> int | None:
        """Devuelve la generación en la que obtuvo el lock, o None."""
        async with self._mutex:
            conn = await self._ensure()
            got = await conn.fetchval("select pg_try_advisory_lock(hashtext($1))", key)
            return self.generation if got else None

    async def still_held(self, generation: int) -> bool:
        async with self._mutex:
            if self.conn is None or self.conn.is_closed() or self.generation != generation:
                return False
            try:
                await self.conn.execute("select 1")
                return True
            except Exception:
                try:
                    await self.conn.close()
                except Exception:
                    log.debug("cierre de la conexión de locks falló", exc_info=True)
                return False

    async def unlock(self, key: str, generation: int) -> None:
        async with self._mutex:
            if self.conn is not None and not self.conn.is_closed() and self.generation == generation:
                try:
                    await self.conn.fetchval("select pg_advisory_unlock(hashtext($1))", key)
                except Exception:
                    log.debug("unlock falló", exc_info=True)

    async def close(self) -> None:
        async with self._mutex:
            if self.conn is not None and not self.conn.is_closed():
                await self.conn.close()
            self.conn = None


SHARED = LockSession()


async def run_as_leader(name: str, fn: Callable[[], Awaitable[None]], session: LockSession | None = None,
                        lock_retry: float = LOCK_RETRY, check_every: float = CHECK_EVERY) -> None:
    """Corre `fn` (una tarea que normalmente no termina) solo mientras este proceso sea el líder de `name`."""
    session = session or SHARED
    key = f"wa_loop:{name}"
    st = STATUS.setdefault(name, {})
    st.setdefault("leader", False)
    st.setdefault("restarts", 0)
    backoff = 1.0
    while True:
        task: asyncio.Task | None = None
        generation = None
        try:
            generation = await session.try_lock(key)
            if generation is None:
                st["leader"] = False
                await asyncio.sleep(lock_retry)
                continue
            st.update(leader=True, since=_now())
            log.info("Tarea %s: este proceso es el líder", name)
            task = asyncio.create_task(fn())
            while True:
                done, _ = await asyncio.wait({task}, timeout=check_every)
                if done:
                    task.result()  # propaga el error; si terminó sin error, se vuelve a lanzar
                    break
                if not await session.still_held(generation):
                    log.warning("Tarea %s: se perdió la conexión del lock; se detiene", name)
                    break
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st.update(last_error=f"{type(e).__name__}: {e}"[:500], last_error_at=_now())
            st["restarts"] = st.get("restarts", 0) + 1
            metrics.loop_restarts.inc(name)
            log.exception("Tarea %s falló; se reinicia en %.0f s", name, backoff)
        finally:
            st["leader"] = False
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if generation is not None:
                await asyncio.shield(session.unlock(key, generation))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def leaders() -> dict:
    return {name: (1 if st.get("leader") else 0) for name, st in STATUS.items()}


metrics.Gauge("wa_loop_leader", "1 si este proceso corre la tarea de fondo", ("loop",),
              fn=lambda: {(k,): v for k, v in leaders().items()})
metrics.Gauge("wa_loop_seconds_since_ok", "Segundos desde la última vuelta correcta (tareas que lo reportan)",
              ("loop",), fn=lambda: {(k,): time.monotonic() - st["last_ok_mono"]
                                     for k, st in STATUS.items() if "last_ok_mono" in st})
