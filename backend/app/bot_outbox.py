"""Agrupa los mensajes de texto del bot que salen seguidos a una misma conversación (optimización de costos).

Con `ai_agents.cost_optimization` activo, los textos que el agente de IA produce para la misma conversación dentro de
una ventana corta (`bot_merge_window_s`, ~2 s) se envían como UN solo mensaje de WhatsApp: el cliente recibe lo mismo
y se factura un mensaje en vez de varios. Imágenes, productos, botones y plantillas nunca se agrupan.

Uso: `await queue_text(session, conv, text, ai_agent_id=…)`; antes de una acción que debe ir después del texto
(transferir, cerrar) llamar `await flush(conv.id)` para enviarlo ya y conservar el orden.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Conversation

log = logging.getLogger(__name__)


@dataclass
class _Buffer:
    organization_id: int
    ai_agent_id: int | None
    parts: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


_buffers: dict[int, _Buffer] = {}
_locks: dict[int, asyncio.Lock] = {}


def _lock(conversation_id: int) -> asyncio.Lock:
    return _locks.setdefault(conversation_id, asyncio.Lock())


def window_s() -> float:
    return float(getattr(get_settings(), "bot_merge_window_s", 2.0))


async def queue_text(session: AsyncSession, conv: Conversation, text: str, *, ai_agent_id: int | None,
                     merge: bool) -> None:
    """Envía `text` ya (merge=False) o lo acumula en la ventana de la conversación (merge=True)."""
    text = (text or "").strip()
    if not text:
        return
    if not merge:
        from app.service import send_text

        await send_text(session, conv, text, sender_type="bot", ai_agent_id=ai_agent_id)
        return
    async with _lock(conv.id):
        buf = _buffers.get(conv.id)
        if buf is None:
            buf = _buffers[conv.id] = _Buffer(organization_id=conv.organization_id, ai_agent_id=ai_agent_id)
            buf.task = asyncio.create_task(_flush_later(conv.id))
        buf.parts.append(text)


async def _flush_later(conversation_id: int) -> None:
    try:
        await asyncio.sleep(window_s())
    except asyncio.CancelledError:
        return
    await flush(conversation_id, cancel_timer=False)


async def flush(conversation_id: int, cancel_timer: bool = True) -> None:
    """Envía lo acumulado para la conversación (si hay algo) como un solo mensaje."""
    async with _lock(conversation_id):
        buf = _buffers.pop(conversation_id, None)
    if buf is None or not buf.parts:
        return
    if cancel_timer and buf.task and not buf.task.done() and buf.task is not asyncio.current_task():
        buf.task.cancel()
    from app.db import SessionLocal, set_actor
    from app.service import send_text

    try:
        async with SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            await set_actor(session, "bot")
            await send_text(session, conv, "\n\n".join(buf.parts), sender_type="bot", ai_agent_id=buf.ai_agent_id)
            await session.commit()
    except Exception:  # nunca rompe el flujo del agente
        log.exception("No se pudo enviar el mensaje agrupado de la conversación %s", conversation_id)


def pending(conversation_id: int) -> list[str]:
    """Textos aún sin enviar (pruebas / diagnóstico)."""
    buf = _buffers.get(conversation_id)
    return list(buf.parts) if buf else []
