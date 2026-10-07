"""Disparadores del copiloto. Nunca lanzan ni bloquean al llamador (ingest, transferencia, cierre).

- on_inbound: mensaje del cliente en una conversación con asesor → espera `debounce_seconds` (ráfagas) y, si fue el
  último mensaje y el asesor no está escribiendo, genera sugerencias.
- on_handoff: resumen para el asesor que recibe la transferencia.
- on_close: resumen de la conversación para el historial.
Corre en tareas de asyncio del proceso que recibió el evento: es trabajo sensible a la latencia (el asesor espera
las sugerencias) y cada evento llega a una sola réplica. El antirrebote vive en memoria de esa réplica.
"""

import asyncio
import logging
import time

from app.db import SessionLocal
from app.models import Conversation

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_pending: dict[int, asyncio.Task] = {}  # conversación → tarea con antirrebote
_typing: dict[int, float] = {}  # conversación → último «el asesor está escribiendo»
TYPING_WINDOW_S = 6
DELAY_OVERRIDE: float | None = None  # pruebas: antirrebote fijo (p. ej. 0)


def _spawn(coro) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # sin event loop (scripts): no se generan sugerencias
        coro.close()
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def agent_typing(conversation_id: int) -> None:
    _typing[conversation_id] = time.monotonic()


def is_typing(conversation_id: int) -> bool:
    return time.monotonic() - _typing.get(conversation_id, 0) < TYPING_WINDOW_S


async def _debounced(conversation_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        for _ in range(3):  # el asesor está escribiendo: espera un poco (no interrumpe con sugerencias)
            if not is_typing(conversation_id):
                break
            await asyncio.sleep(TYPING_WINDOW_S)
        async with SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None or conv.status != "human" or not conv.assigned_agent_id:
                return
            from app.copilot import core

            cfg = await core.settings(session, conv.organization_id)
            if not cfg.get("enabled") or cfg.get("suggestions") != "auto":
                return
            await core.generate(session, conv)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("El copiloto no pudo generar sugerencias para la conversación %s", conversation_id)
    finally:
        if _pending.get(conversation_id) is asyncio.current_task():
            _pending.pop(conversation_id, None)


def on_inbound(conversation_id: int, message_id: int, delay: float | None = None) -> None:
    """Programa (o reprograma) las sugerencias para este mensaje; cancela la ráfaga anterior."""
    try:
        prev = _pending.pop(conversation_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        task = _spawn(_resolve_delay_and_run(conversation_id, message_id, delay))
        if task is not None:
            _pending[conversation_id] = task
    except Exception:  # noqa: BLE001
        log.exception("Copiloto: no se pudo programar la sugerencia")


async def _resolve_delay_and_run(conversation_id: int, message_id: int, delay: float | None) -> None:
    if DELAY_OVERRIDE is not None:
        delay = DELAY_OVERRIDE
    if delay is None:
        try:
            async with SessionLocal() as session:
                conv = await session.get(Conversation, conversation_id)
                if conv is None or conv.status != "human":
                    return
                from app.copilot import core

                delay = float((await core.settings(session, conv.organization_id)).get("debounce_seconds") or 0)
        except Exception:  # noqa: BLE001
            log.exception("Copiloto: configuración no disponible")
            return
    await _debounced(conversation_id, message_id, delay)


async def _summary(conversation_id: int, kind: str) -> None:
    try:
        async with SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            from app.copilot import core

            if kind == "handoff":
                await core.handoff_summary(session, conv)
            else:
                await core.conversation_summary(session, conv, on_close=True)
    except Exception:  # noqa: BLE001
        log.exception("Copiloto: resumen (%s) falló para la conversación %s", kind, conversation_id)


def on_handoff(conversation_id: int) -> None:
    try:
        _spawn(_summary(conversation_id, "handoff"))
    except Exception:  # noqa: BLE001
        log.exception("Copiloto: no se pudo programar el resumen de traspaso")


def on_close(conversation_id: int) -> None:
    try:
        _spawn(_summary(conversation_id, "close"))
    except Exception:  # noqa: BLE001
        log.exception("Copiloto: no se pudo programar el resumen de cierre")


async def drain() -> None:
    """Pruebas: espera a que terminen las tareas pendientes."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
