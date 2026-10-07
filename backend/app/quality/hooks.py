"""Disparadores de calidad en segundo plano. Nunca lanzan hacia el llamador.

- on_close(conversation_id): revisión automática al cerrar (service.close).
- on_agent_changed(org, ai_agent_id): corre las suites con run_on_change del agente (routers/bots.py).
- quality_loop(): cada 5 min revisa conversaciones cerradas (últimos 3 días) que no tengan su revisión — cubre
  cierres que no pasan por service.close (inactividad, triggers) y reinicios del servidor.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import exists, func, select

from app.db import SessionLocal
from app.models import AgentTestSuite, Conversation, ConversationReview, Organization, QAScorecard, utcnow
from app.plans import has_feature

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
LOOP_EVERY_S = 300
LOOKBACK = timedelta(days=3)
AGENT_CHANGE_DELAY_S = 20  # agrupa varios guardados seguidos del mismo agente
_pending_agent_runs: dict[int, asyncio.Task] = {}


def _spawn(coro, name: str) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:  # sin loop (scripts)
        coro.close()
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def review_closed(conversation_id: int) -> int:
    from app.quality.reviews import review_conversation

    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation_id)
        if not conv or conv.status != "closed":
            return 0
        if not await has_feature(session, conv.organization_id, "qa"):
            return 0
        return len(await review_conversation(session, conv))


def on_close(conversation_id: int) -> None:
    async def job():
        try:
            await review_closed(conversation_id)
        except Exception:
            log.exception("Revisión QA al cerrar la conversación %s", conversation_id)

    _spawn(job(), f"qa-review-{conversation_id}")


def on_agent_changed(organization_id: int, ai_agent_id: int) -> None:
    """Pruebas al cambiar un agente de IA (con espera para agrupar guardados seguidos)."""
    async def job():
        await asyncio.sleep(AGENT_CHANGE_DELAY_S)
        try:
            from app.quality.agent_tests import run_suite

            async with SessionLocal() as session:
                if not await has_feature(session, organization_id, "qa"):
                    return
                suites = (await session.scalars(select(AgentTestSuite.id).where(
                    AgentTestSuite.organization_id == organization_id, AgentTestSuite.ai_agent_id == ai_agent_id,
                    AgentTestSuite.run_on_change))).all()
            for suite_id in suites:
                await run_suite(suite_id, trigger="on_change")
        except Exception:
            log.exception("Pruebas del agente de IA %s", ai_agent_id)
        finally:
            _pending_agent_runs.pop(ai_agent_id, None)

    prev = _pending_agent_runs.pop(ai_agent_id, None)
    if prev and not prev.done():
        prev.cancel()
    task = _spawn(job(), f"agent-tests-{ai_agent_id}")
    if task:
        _pending_agent_runs[ai_agent_id] = task


async def review_pending(limit: int = 50) -> int:
    """Conversaciones cerradas recientes de empresas con QA, con rúbricas automáticas y sin revisión de IA."""
    since = utcnow() - LOOKBACK
    async with SessionLocal() as session:
        has_review = exists().where(ConversationReview.conversation_id == Conversation.id,
                                    ConversationReview.reviewer_type == "ai")
        # Misma muestra que reviews.in_sample, en SQL: las que nunca se evaluarán no ocupan la ventana
        has_scorecard = exists().where(QAScorecard.organization_id == Conversation.organization_id,
                                       QAScorecard.is_active, QAScorecard.auto_review,
                                       Conversation.message_count >= QAScorecard.min_messages,
                                       func.mod(Conversation.id * 2654435761, 100) < QAScorecard.sample_pct)
        rows = (await session.execute(
            select(Conversation.id, Conversation.organization_id)
            .where(Conversation.status == "closed", Conversation.closed_at >= since,
                   Conversation.closed_at < utcnow() - timedelta(minutes=2), ~has_review, has_scorecard)
            .order_by(Conversation.closed_at.desc()).limit(limit * 10))).all()
        allowed: dict[int, bool] = {}
        for _cid, org in rows:
            if org not in allowed:
                o = await session.get(Organization, org)
                allowed[org] = bool(o) and o.status not in ("suspended", "cancelled") and \
                    await has_feature(session, org, "qa")
    done = 0
    for cid, org in rows:
        if done >= limit:
            break
        if allowed.get(org):
            # Las que no entran en la muestra no generan revisión: review_closed devuelve 0 y no cuenta
            done += await review_closed(cid)
    return done


async def quality_loop() -> None:
    while True:
        await asyncio.sleep(LOOP_EVERY_S)
        try:
            n = await review_pending()
            if n:
                log.info("QA: %s revisiones automáticas", n)
        except Exception:
            log.exception("Bucle de calidad (QA)")
