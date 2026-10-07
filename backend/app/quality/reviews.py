"""Revisión de calidad de conversaciones (QA) con la IA o un supervisor. docs/data-model.md §12.3.

Una conversación cerrada se evalúa contra cada rúbrica activa que le aplique:
- sujeto «agent» si un asesor escribió (se evalúa al último asesor que escribió) y «bot» si respondió el bot;
- rúbrica activa, con revisión automática, del sujeto (o «any»), del grupo de la conversación, con el mínimo de
  mensajes y dentro de la muestra (determinística por id de conversación).
La IA devuelve puntaje 0-100 por criterio (con evidencia), sentimiento, esfuerzo del cliente, resumen y sugerencias
de coaching. El total es el promedio ponderado de los criterios que aplican; un criterio crítico < 50 marca
`critical_failed`. Una sola revisión de IA por conversación y rúbrica (idempotente; `force` la rehace).

Privacidad: al LLM solo va texto (los archivos se describen como «[imagen]», «[documento]»...), sin números de
teléfono ni correos, y con un tope de longitud.
"""

import logging
import re

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import CallContext, complete_json, resolve_cortex
from app.ai.structured import LLMError
from app.db import set_actor
from app.models import (
    Agent,
    CoachingItem,
    Conversation,
    ConversationReview,
    Message,
    QAScorecard,
    utcnow,
)

log = logging.getLogger(__name__)

MAX_TRANSCRIPT = 16000
CRITICAL_THRESHOLD = 50
COACHING_BELOW = 80     # solo se sugiere coaching en criterios con puntaje menor
MAX_COACHING = 3
SENTIMENTS = ("positive", "neutral", "negative", "mixed")
MEDIA_LABEL = {"image": "imagen", "audio": "audio", "video": "video", "document": "documento", "sticker": "sticker",
               "location": "ubicación", "contacts": "contacto", "product": "producto", "template": "plantilla"}
PHONE_RE = re.compile(r"\+?\d[\d\s\-]{7,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


class ReviewError(Exception):
    pass


def in_sample(conversation_id: int, pct: int) -> bool:
    """Muestra determinística: la misma conversación siempre cae (o no) en la muestra."""
    return (conversation_id * 2654435761) % 100 < pct


def redact(text: str) -> str:
    return EMAIL_RE.sub("[correo]", PHONE_RE.sub("[número]", text or ""))


async def participants(session: AsyncSession, conv: Conversation) -> dict:
    """{agent_id, has_agent, has_bot, ai_agent_id} de la conversación."""
    rows = (await session.execute(
        select(Message.sender_type, Message.sender_agent_id, Message.ai_agent_id)
        .where(Message.conversation_id == conv.id, Message.direction == "out")
        .order_by(Message.created_at, Message.id))).all()
    agent_ids = [a for t, a, _ in rows if t == "agent" and a]
    bot_ids = [b for t, _, b in rows if t == "bot"]
    return {"has_agent": any(t == "agent" for t, _, _ in rows), "has_bot": bool(bot_ids),
            "agent_id": agent_ids[-1] if agent_ids else conv.assigned_agent_id,
            "ai_agent_id": next((b for b in reversed(bot_ids) if b), None) or conv.ai_agent_id}


def scorecard_applies(sc: QAScorecard, conv: Conversation, subject: str, auto: bool) -> bool:
    if not sc.is_active or (auto and not sc.auto_review):
        return False
    if sc.applies_to not in (subject, "any"):
        return False
    if sc.group_ids and conv.group_id not in sc.group_ids:
        return False
    if (conv.message_count or 0) < (sc.min_messages or 0):
        return False
    return not auto or in_sample(conv.id, sc.sample_pct)


async def plan_reviews(session: AsyncSession, conv: Conversation, auto: bool = True,
                       scorecard_id: int | None = None) -> list[tuple[QAScorecard, str]]:
    """(rúbrica, sujeto) a evaluar en esta conversación."""
    who = await participants(session, conv)
    subjects = [s for s, ok in (("agent", who["has_agent"]), ("bot", who["has_bot"])) if ok]
    q = select(QAScorecard).where(QAScorecard.organization_id == conv.organization_id)
    if scorecard_id:
        q = q.where(QAScorecard.id == scorecard_id)
    out = []
    for sc in (await session.scalars(q.order_by(QAScorecard.id))).all():
        # «any» evalúa al asesor si hubo uno; si no, al bot
        for subject in subjects:
            if scorecard_applies(sc, conv, subject, auto) or (scorecard_id and sc.applies_to in (subject, "any")):
                out.append((sc, subject))
                break
    return out


async def transcript(session: AsyncSession, conv: Conversation) -> str:
    msgs = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                  .order_by(Message.created_at, Message.id))).all()
    names = dict((await session.execute(select(Agent.id, Agent.name)
                                        .where(Agent.organization_id == conv.organization_id))).all())
    lines = []
    for m in msgs:
        if m.sender_type == "contact":
            who = "Cliente"
        elif m.sender_type == "agent":
            who = f"Asesor ({names.get(m.sender_agent_id, 'sin nombre')})"
        elif m.sender_type == "bot":
            who = "Bot"
        elif m.sender_type == "system":
            who = "Sistema"
        else:
            who = "Mensaje automático"
        body = m.transcript if m.type == "audio" and m.transcript else (m.text or "")
        if m.type in MEDIA_LABEL and m.type != "text":
            body = f"[{MEDIA_LABEL[m.type]}] {body}".strip()
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {who}: {redact(body)}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT:  # inicio y final (el cierre pesa en varios criterios)
        text = text[:4000] + "\n[… conversación recortada …]\n" + text[-(MAX_TRANSCRIPT - 4000):]
    return text


def build_schema(criteria: list[dict]) -> dict:
    keys = [c["key"] for c in criteria]
    score = {"type": "object", "additionalProperties": False,
             "required": ["key", "applies", "score", "evidence", "comment"],
             "properties": {"key": {"type": "string", "enum": keys}, "applies": {"type": "boolean"},
                            "score": {"type": "integer"}, "evidence": {"type": "string"},
                            "comment": {"type": "string"}}}
    coaching = {"type": "object", "additionalProperties": False,
                "required": ["criterion_key", "title", "suggestion", "example"],
                "properties": {"criterion_key": {"type": "string", "enum": keys}, "title": {"type": "string"},
                               "suggestion": {"type": "string"}, "example": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["scores", "sentiment", "sentiment_score", "customer_effort", "summary", "coaching"],
            "properties": {"scores": {"type": "array", "items": score},
                           "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
                           "sentiment_score": {"type": "number"}, "customer_effort": {"type": "integer"},
                           "summary": {"type": "string"}, "coaching": {"type": "array", "items": coaching}}}


def build_system(sc: QAScorecard, subject: str) -> str:
    who = "al ASESOR humano" if subject == "agent" else "al BOT (asistente de IA)"
    rubric = "\n".join(f"- {c['key']} — {c['label']} (peso {c['weight']}{', CRÍTICO' if c.get('critical') else ''}): "
                       f"{c.get('description') or ''}" for c in sc.criteria)
    return (
        "Eres auditor de calidad de atención al cliente por chat (WhatsApp y otros canales) de una empresa en "
        f"Latinoamérica. Evalúa SOLO {who} en la conversación, con esta rúbrica:\n{rubric}\n\n"
        "Reglas:\n"
        "- Puntúa cada criterio de 0 a 100 (100 = excelente, 50 = aceptable con fallas claras, 0 = no lo hizo o lo "
        "hizo mal).\n"
        "- applies = false si el criterio no aplica a esta conversación (por ejemplo, no hubo objeciones); en ese "
        "caso score = 100.\n"
        "- evidence: cita textual breve de la conversación que justifica el puntaje (vacío si no aplica).\n"
        "- Los tiempos se ven en las horas de cada mensaje.\n"
        "- sentiment: cómo terminó el cliente; sentiment_score entre -1 y 1; customer_effort de 1 (fácil) a 5 "
        "(le costó mucho).\n"
        "- summary: 1 o 2 frases en español.\n"
        f"- coaching: hasta {MAX_COACHING} sugerencias concretas para mejorar los criterios más bajos, con una frase "
        "de ejemplo de cómo decirlo mejor. Vacío si todo estuvo bien.\n"
        "- No inventes hechos que no estén en la conversación."
    )


def score_review(criteria: list[dict], scores: dict) -> tuple[float | None, bool]:
    """(total ponderado 0-100 de los criterios que aplican, ¿falló un crítico?)."""
    total_w = total = 0.0
    critical_failed = False
    for c in criteria:
        s = scores.get(c["key"])
        if not s or not s.get("applies", True) or s.get("score") is None:
            continue
        val = max(0, min(100, int(s["score"])))
        total += val * c["weight"]
        total_w += c["weight"]
        if c.get("critical") and val < CRITICAL_THRESHOLD:
            critical_failed = True
    return (round(total / total_w, 2) if total_w else None), critical_failed


def normalize_scores(criteria: list[dict], raw: list | dict) -> dict:
    """Lista o dict del modelo/supervisor → {key: {score, applies, evidence, comment}} solo con criterios válidos."""
    keys = {c["key"] for c in criteria}
    items = raw.items() if isinstance(raw, dict) else ((r.get("key"), r) for r in raw or [] if isinstance(r, dict))
    out = {}
    for key, s in items:
        if key not in keys or not isinstance(s, dict):
            continue
        try:
            score = max(0, min(100, int(s.get("score"))))
        except (TypeError, ValueError):
            continue
        out[key] = {"score": score, "applies": bool(s.get("applies", True)),
                    "evidence": str(s.get("evidence") or "")[:500], "comment": str(s.get("comment") or "")[:500]}
    return out


async def finalize(session: AsyncSession, conv: Conversation) -> None:
    """La conversación muestra la última revisión terminada (prioriza la del asesor)."""
    latest = (await session.scalars(
        select(ConversationReview).where(ConversationReview.conversation_id == conv.id,
                                         ConversationReview.status.in_(("done", "disputed")))
        .order_by((ConversationReview.subject_type == "agent").desc(), ConversationReview.updated_at.desc())
        .limit(1))).first()
    if latest is None:
        return
    await set_actor(session, "system")
    conv.qa_score = latest.total_score
    if latest.sentiment:
        conv.ai_sentiment = "neutral" if latest.sentiment == "mixed" else latest.sentiment


async def _coaching(session: AsyncSession, review: ConversationReview, suggestions: list) -> None:
    await session.execute(delete(CoachingItem).where(CoachingItem.review_id == review.id,
                                                     CoachingItem.status == "open"))
    if review.subject_type != "agent" or not review.agent_id:
        return
    added = 0
    for s in suggestions or []:
        if not isinstance(s, dict) or added >= MAX_COACHING:
            break
        key = s.get("criterion_key")
        score = (review.scores.get(key) or {}).get("score")
        if score is not None and score >= COACHING_BELOW:
            continue
        if not str(s.get("suggestion") or "").strip():
            continue
        session.add(CoachingItem(organization_id=review.organization_id, agent_id=review.agent_id,
                                 review_id=review.id, criterion_key=key,
                                 title=str(s.get("title") or "Sugerencia")[:160],
                                 suggestion=str(s["suggestion"])[:1000], example=str(s.get("example") or "")[:500] or None))
        added += 1


async def review_with_ai(session: AsyncSession, conv: Conversation, sc: QAScorecard, subject: str,
                         force: bool = False) -> ConversationReview:
    """Evalúa (o reevalúa con force) la conversación con la rúbrica. Nunca lanza por errores del LLM: la revisión
    queda en «failed» con el error."""
    existing = (await session.scalars(select(ConversationReview).where(
        ConversationReview.conversation_id == conv.id, ConversationReview.scorecard_id == sc.id,
        ConversationReview.reviewer_type == "ai"))).first()
    if existing and not force:
        return existing
    who = await participants(session, conv)
    review = existing or ConversationReview(organization_id=conv.organization_id, conversation_id=conv.id,
                                            scorecard_id=sc.id, reviewer_type="ai")
    review.subject_type = subject
    review.agent_id = who["agent_id"] if subject == "agent" else None
    review.ai_agent_id = who["ai_agent_id"] if subject == "bot" else None
    review.dispute_note = None if force else review.dispute_note

    ctx = CallContext(organization_id=conv.organization_id, purpose="qa", conversation_id=conv.id,
                      ai_agent_id=review.ai_agent_id)
    try:
        text = await transcript(session, conv)
        cx = await resolve_cortex(session, conv.organization_id, None, "qa")
        data = await complete_json(session, cx, build_system(sc, subject), f"Conversación:\n{text}",
                                   build_schema(sc.criteria), ctx, max_tokens=3000)
        review.scores = normalize_scores(sc.criteria, data.get("scores") or [])
        review.total_score, review.critical_failed = score_review(sc.criteria, review.scores)
        review.sentiment = data.get("sentiment") if data.get("sentiment") in SENTIMENTS else None
        try:
            review.sentiment_score = max(-1.0, min(1.0, float(data.get("sentiment_score"))))
        except (TypeError, ValueError):
            review.sentiment_score = None
        try:
            review.customer_effort = max(1, min(5, int(data.get("customer_effort"))))
        except (TypeError, ValueError):
            review.customer_effort = None
        review.summary = str(data.get("summary") or "")[:1000] or None
        review.status, review.error = "done", None
        suggestions = data.get("coaching") or []
    except (LLMError, ReviewError) as e:
        log.warning("Revisión QA fallida (conversación %s, rúbrica %s): %s", conv.id, sc.id, e)
        review.status, review.error, suggestions = "failed", str(e)[:1000], []
    review.ai_call_id = ctx.call_ids[-1] if ctx.call_ids else None
    review.updated_at = utcnow()
    session.add(review)
    try:
        await session.flush()
    except IntegrityError:  # otra tarea la creó al mismo tiempo
        await session.rollback()
        return (await session.scalars(select(ConversationReview).where(
            ConversationReview.conversation_id == conv.id, ConversationReview.scorecard_id == sc.id,
            ConversationReview.reviewer_type == "ai"))).one()
    if review.status == "done":
        await _coaching(session, review, suggestions)
        await finalize(session, conv)
    await session.commit()
    return review


async def review_conversation(session: AsyncSession, conv: Conversation, auto: bool = True, force: bool = False,
                              scorecard_id: int | None = None) -> list[ConversationReview]:
    if conv.status != "closed" and auto:
        return []
    return [await review_with_ai(session, conv, sc, subject, force=force)
            for sc, subject in await plan_reviews(session, conv, auto=auto, scorecard_id=scorecard_id)]


async def human_review(session: AsyncSession, conv: Conversation, sc: QAScorecard, reviewer: Agent, scores: dict,
                       summary: str | None, sentiment: str | None) -> ConversationReview:
    who = await participants(session, conv)
    subject = "agent" if who["has_agent"] and sc.applies_to in ("agent", "any") else "bot"
    review = ConversationReview(organization_id=conv.organization_id, conversation_id=conv.id, scorecard_id=sc.id,
                                reviewer_type="human", reviewer_agent_id=reviewer.id, subject_type=subject,
                                agent_id=who["agent_id"] if subject == "agent" else None,
                                ai_agent_id=who["ai_agent_id"] if subject == "bot" else None, status="done",
                                summary=(summary or "").strip()[:1000] or None,
                                sentiment=sentiment if sentiment in SENTIMENTS else None)
    review.scores = normalize_scores(sc.criteria, scores)
    if not review.scores:
        raise ReviewError("Indica el puntaje de al menos un criterio")
    review.total_score, review.critical_failed = score_review(sc.criteria, review.scores)
    session.add(review)
    await session.flush()
    await finalize(session, conv)
    await session.commit()
    return review
