"""Recuperación por inactividad del agente de IA (docs/data-model.md §17).

Para conversaciones atendidas por el bot en las que el cliente dejó de responder: hasta 3 intentos (cada uno
`after_hours` después del anterior; el primero desde el último mensaje del cliente), escritos por la IA con el
contexto de la conversación o literales; después de `inactivity_end_hours` sin respuesta se cierra con la tipificación
configurada. Nunca se escribe fuera de la ventana de atención (24 h desde el último mensaje del cliente; 72 h si la
conversación empezó por un anuncio Click to WhatsApp): ahí solo se podría con plantilla (de pago).
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import router
from app.ai.base import AgentRequest
from app.db import SessionLocal, set_actor
from app.models import AIAgent, Attribution, Channel, Conversation, Message, Typification, utcnow

log = logging.getLogger(__name__)
SERVICE_WINDOW = timedelta(hours=24)
CTWA_WINDOW = timedelta(hours=72)
MAX_PER_TICK = 200


async def window_end(session: AsyncSession, conv: Conversation):
    """Hasta cuándo se puede escribir sin plantilla (y sin costo)."""
    if conv.last_inbound_at is None:
        return None
    window = SERVICE_WINDOW
    if conv.channel.provider == "whatsapp_cloud":
        attr = await session.scalar(select(Attribution).where(Attribution.conversation_id == conv.id))
        # Ventana de entrada gratuita de Click to WhatsApp: 72 h desde que el negocio responde al anuncio
        if attr and attr.channel == "meta_ctwa" and attr.ctwa_clid and \
                utcnow() - (attr.created_at or utcnow()) < CTWA_WINDOW:
            window = CTWA_WINDOW
    elif conv.channel.provider == "webchat":
        return None  # el chat web no tiene ventana
    return conv.last_inbound_at + window


async def _ai_text(session: AsyncSession, conv: Conversation, agent: AIAgent, guidance: str, attempt: int) -> str:
    from app.agent import build_turns

    rows = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                  .order_by(Message.created_at.desc()).limit(30))).all()
    turns = build_turns(list(reversed(rows)))
    if not turns:
        return guidance
    system = (agent.system_prompt + "\n\n## Tarea\nEl cliente dejó de responder. Escribe UN mensaje breve (máx. 40 "
              "palabras) para retomar la conversación usando su contexto: menciona lo que estaba buscando, sin "
              "presionar y sin repetir mensajes anteriores. Responde solo con el texto del mensaje.")
    req = AgentRequest(model="", system=system, turns=turns, tools=[], max_tokens=300,
                       context=f"Intento de recuperación {attempt}. Guía: {guidance}")
    cx = await router.resolve_cortex(session, conv.organization_id, agent.cortex_id, "chat")
    ctx = router.CallContext(organization_id=conv.organization_id, purpose="chat", conversation_id=conv.id,
                             ai_agent_id=agent.id)
    result = await router.run_chat(session, cx, req, lambda *_: None, ctx)
    return (result.text or "").strip() or guidance


async def process_conversation(session: AsyncSession, conv: Conversation, agent: AIAgent) -> str | None:
    """Envía el intento que toque o cierra por inactividad. Devuelve lo que hizo."""
    from app.agent_config import log_event
    from app.service import close, send_text

    if conv.status != "bot" or conv.last_inbound_at is None:
        return None
    last = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                  .order_by(Message.created_at.desc()).limit(1))).first()
    if last is None or last.direction == "in":
        return None  # el bot aún no respondió el último mensaje: no es inactividad del cliente
    now = utcnow()
    attempts = agent.recovery_attempts or []
    sent = conv.recovery_attempts_sent or 0
    reference = conv.last_recovery_at if sent and conv.last_recovery_at else conv.last_inbound_at
    if sent < len(attempts):
        attempt = attempts[sent]
        if now - reference < timedelta(hours=float(attempt["after_hours"])):
            return None
        end = await window_end(session, conv)
        if end is not None and now >= end:
            # Fuera de la ventana gratuita: no se insiste (requeriría una plantilla de pago)
            conv.recovery_attempts_sent = len(attempts)
            conv.last_recovery_at = now
            await session.commit()
            return "skipped_window"
        message = attempt["message"]
        if attempt.get("use_ai", True):
            try:
                message = await _ai_text(session, conv, agent, attempt["message"], sent + 1)
            except Exception:  # noqa: BLE001 — si la IA falla se usa el texto de la guía
                log.exception("Recuperación con IA falló en la conversación %s", conv.id)
        await set_actor(session, "bot")
        await send_text(session, conv, message, sender_type="bot", ai_agent_id=agent.id)
        conv.recovery_attempts_sent = sent + 1
        conv.last_recovery_at = now
        await log_event(session, conv.id, "recovery_sent", {"attempt": sent + 1, "ai": bool(attempt.get("use_ai", True))})
        await session.commit()
        return f"attempt_{sent + 1}"
    if agent.inactivity_end_hours is None:
        return None
    if now - reference < timedelta(hours=float(agent.inactivity_end_hours)):
        return None
    typ = await session.get(Typification, agent.inactivity_end_typification_id) \
        if agent.inactivity_end_typification_id else None
    await set_actor(session, "automation")
    await log_event(session, conv.id, "inactivity_closed", {"attempts": sent})
    await close(session, conv, typ, actor="automation")
    return "closed"


async def run_recovery() -> int:
    done = 0
    async with SessionLocal() as session:
        agents = {a.id: a for a in (await session.scalars(select(AIAgent).where(AIAgent.recovery_enabled))).all()}
        if not agents:
            return 0
        default_by_channel = dict((await session.execute(select(Channel.id, Channel.default_ai_agent_id))).all())
        convs = (await session.scalars(select(Conversation).where(
            Conversation.status == "bot", Conversation.last_inbound_at.is_not(None),
            Conversation.last_inbound_at > utcnow() - timedelta(days=4))
            .order_by(Conversation.last_inbound_at).limit(MAX_PER_TICK * 5))).unique().all()
        for conv in convs:
            agent = agents.get(conv.ai_agent_id or default_by_channel.get(conv.channel_id))
            if agent is None or agent.organization_id != conv.organization_id:
                continue
            try:
                if await process_conversation(session, conv, agent):
                    done += 1
            except Exception:  # noqa: BLE001 — una conversación con error no detiene las demás
                log.exception("Recuperación falló en la conversación %s", conv.id)
                await session.rollback()
            if done >= MAX_PER_TICK:
                break
    return done


async def recovery_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            n = await run_recovery()
            if n:
                log.info("Recuperación por inactividad: %s acciones", n)
        except Exception:
            log.exception("Falló la recuperación por inactividad")


def handled_by_recovery(conv: Conversation, agents_with_recovery: set[int], default_agent: int | None) -> bool:
    """El cierre por inactividad genérico no toca conversaciones del bot cuyo agente tiene recuperación propia."""
    return conv.status == "bot" and (conv.ai_agent_id or default_agent) in agents_with_recovery
