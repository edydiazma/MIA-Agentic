"""Calidad (QA) y coaching: rúbricas, revisiones de conversaciones, disputas, coaching y reporte. §12.3."""

from collections import Counter, defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import (
    Agent,
    AIAgent,
    CoachingItem,
    Contact,
    Conversation,
    ConversationReview,
    QAScorecard,
    utcnow,
)
from app.plans import feature_required
from app.quality import coaching as coach
from app.quality.defaults import clean_criteria, ensure_defaults
from app.quality.reviews import ReviewError, finalize, human_review, review_conversation, score_review
from app.routers.reports import _days, _pct, _range, _rows

router = APIRouter(prefix="/api", tags=["quality"], dependencies=[Depends(feature_required("qa"))])
SUPERVISORS = ("admin", "supervisor")


def is_supervisor(agent: Agent) -> bool:
    return agent.role in SUPERVISORS


async def require_supervisor(agent: Agent = Depends(current_agent)) -> Agent:
    if not is_supervisor(agent):
        raise HTTPException(403, "Solo supervisores o administradores")
    return agent


# --- Rúbricas -------------------------------------------------------------------
class ScorecardIn(BaseModel):
    name: str
    applies_to: str = "agent"
    criteria: list[dict]
    auto_review: bool = True
    sample_pct: int = 100
    min_messages: int = 3
    group_ids: list[int] = []
    is_active: bool = True


def scorecard_out(sc: QAScorecard) -> dict:
    return {"id": sc.id, "name": sc.name, "applies_to": sc.applies_to, "criteria": sc.criteria,
            "auto_review": sc.auto_review, "sample_pct": sc.sample_pct, "min_messages": sc.min_messages,
            "group_ids": list(sc.group_ids or []), "is_active": sc.is_active, "created_at": sc.created_at,
            "updated_at": sc.updated_at}


def _check_scorecard(body: ScorecardIn) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "La rúbrica necesita un nombre")
    if body.applies_to not in ("agent", "bot", "any"):
        raise HTTPException(422, "applies_to debe ser agent, bot o any")
    if not 0 <= body.sample_pct <= 100:
        raise HTTPException(422, "La muestra debe estar entre 0 y 100 %")
    return {"name": body.name.strip()[:120], "applies_to": body.applies_to, "criteria": clean_criteria(body.criteria),
            "auto_review": body.auto_review, "sample_pct": body.sample_pct,
            "min_messages": max(0, min(500, body.min_messages)), "group_ids": sorted(set(body.group_ids)),
            "is_active": body.is_active}


async def _scorecard(session: AsyncSession, org: int, sid: int) -> QAScorecard:
    sc = await session.get(QAScorecard, sid)
    if not sc or sc.organization_id != org:
        raise HTTPException(404, "Rúbrica no encontrada")
    return sc


@router.get("/quality/scorecards")
async def list_scorecards(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if await ensure_defaults(session, agent.organization_id):
        await session.commit()
    rows = (await session.scalars(select(QAScorecard).where(QAScorecard.organization_id == agent.organization_id)
                                  .order_by(QAScorecard.id))).all()
    return [scorecard_out(s) for s in rows]


@router.post("/quality/scorecards")
async def create_scorecard(body: ScorecardIn, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    data = _check_scorecard(body)
    if await session.scalar(select(QAScorecard.id).where(QAScorecard.organization_id == agent.organization_id,
                                                         QAScorecard.name == data["name"])):
        raise HTTPException(409, "Ya existe una rúbrica con ese nombre")
    sc = QAScorecard(organization_id=agent.organization_id, created_by=agent.id, **data)
    session.add(sc)
    await session.commit()
    return scorecard_out(sc)


@router.put("/quality/scorecards/{sid}")
async def update_scorecard(sid: int, body: ScorecardIn, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    sc = await _scorecard(session, agent.organization_id, sid)
    data = _check_scorecard(body)
    if await session.scalar(select(QAScorecard.id).where(QAScorecard.organization_id == agent.organization_id,
                                                         QAScorecard.name == data["name"], QAScorecard.id != sid)):
        raise HTTPException(409, "Ya existe una rúbrica con ese nombre")
    for k, v in data.items():
        setattr(sc, k, v)
    sc.updated_at = utcnow()
    await session.commit()
    return scorecard_out(sc)


@router.delete("/quality/scorecards/{sid}")
async def delete_scorecard(sid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Las revisiones hechas se conservan (sin rúbrica)."""
    await session.delete(await _scorecard(session, agent.organization_id, sid))
    await session.commit()
    return {"ok": True}


# --- Revisiones ---------------------------------------------------------------------
async def reviews_out(session: AsyncSession, reviews: list[ConversationReview]) -> list[dict]:
    if not reviews:
        return []
    org = reviews[0].organization_id
    cards = {s.id: s for s in (await session.scalars(select(QAScorecard).where(QAScorecard.organization_id == org))).all()}
    names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
    bots = dict((await session.execute(select(AIAgent.id, AIAgent.name).where(AIAgent.organization_id == org))).all())
    conv_ids = {r.conversation_id for r in reviews}
    contacts = dict((await session.execute(
        select(Conversation.id, Contact.name).join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.id.in_(conv_ids)))).all())
    out = []
    for r in reviews:
        sc = cards.get(r.scorecard_id)
        criteria = sc.criteria if sc else [{"key": k, "label": k, "weight": 0, "critical": False} for k in r.scores]
        out.append({
            "id": r.id, "conversation_id": r.conversation_id, "contact_name": contacts.get(r.conversation_id),
            "scorecard_id": r.scorecard_id, "scorecard_name": sc.name if sc else "(rúbrica eliminada)",
            "reviewer_type": r.reviewer_type, "reviewer_agent_id": r.reviewer_agent_id,
            "reviewer_name": names.get(r.reviewer_agent_id), "subject_type": r.subject_type,
            "agent_id": r.agent_id, "agent_name": names.get(r.agent_id), "ai_agent_id": r.ai_agent_id,
            "ai_agent_name": bots.get(r.ai_agent_id), "status": r.status,
            "scores": [{**{k: c.get(k) for k in ("key", "label", "weight", "critical")},
                        **(r.scores.get(c["key"]) or {"score": None, "applies": False, "evidence": "", "comment": ""})}
                       for c in criteria],
            "total_score": float(r.total_score) if r.total_score is not None else None,
            "critical_failed": r.critical_failed, "sentiment": r.sentiment,
            "sentiment_score": float(r.sentiment_score) if r.sentiment_score is not None else None,
            "customer_effort": r.customer_effort, "summary": r.summary, "error": r.error,
            "dispute_note": r.dispute_note, "ai_call_id": r.ai_call_id, "created_at": r.created_at,
            "updated_at": r.updated_at,
        })
    return out


async def _conversation(session: AsyncSession, org: int, cid: int) -> Conversation:
    conv = await session.get(Conversation, cid)
    if not conv or conv.organization_id != org:
        raise HTTPException(404, "Conversación no encontrada")
    return conv


async def _review(session: AsyncSession, org: int, rid: int) -> ConversationReview:
    r = await session.get(ConversationReview, rid)
    if not r or r.organization_id != org:
        raise HTTPException(404, "Revisión no encontrada")
    return r


@router.get("/quality/reviews")
async def list_reviews(start: date | None = None, end: date | None = None, agent_id: int | None = None,
                       scorecard_id: int | None = None, subject: str | None = None, status: str | None = None,
                       critical: bool | None = None, max_score: float | None = None, reviewer_type: str | None = None,
                       limit: int = 50, offset: int = 0, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    """Los asesores solo ven sus propias revisiones."""
    org = agent.organization_id
    lo, hi, _tz, _s, _e = await _range(session, org, start, end)
    q = select(ConversationReview).where(ConversationReview.organization_id == org,
                                         ConversationReview.created_at >= lo, ConversationReview.created_at < hi)
    if not is_supervisor(agent):
        q = q.where(ConversationReview.agent_id == agent.id)
    elif agent_id:
        q = q.where(ConversationReview.agent_id == agent_id)
    if scorecard_id:
        q = q.where(ConversationReview.scorecard_id == scorecard_id)
    if subject in ("agent", "bot"):
        q = q.where(ConversationReview.subject_type == subject)
    if status:
        q = q.where(ConversationReview.status == status)
    if critical is not None:
        q = q.where(ConversationReview.critical_failed == critical)
    if max_score is not None:
        q = q.where(ConversationReview.total_score <= max_score)
    if reviewer_type in ("ai", "human"):
        q = q.where(ConversationReview.reviewer_type == reviewer_type)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await session.scalars(q.order_by(ConversationReview.created_at.desc())
                                  .limit(max(1, min(limit, 200))).offset(max(0, offset)))).all()
    return {"total": total, "items": await reviews_out(session, list(rows))}


@router.get("/quality/reviews/{rid}")
async def get_review(rid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    r = await _review(session, agent.organization_id, rid)
    if not is_supervisor(agent) and r.agent_id != agent.id:
        raise HTTPException(404, "Revisión no encontrada")
    out = (await reviews_out(session, [r]))[0]
    out["coaching"] = [coach.item_out(i) for i in (await session.scalars(
        select(CoachingItem).where(CoachingItem.review_id == r.id).order_by(CoachingItem.id))).all()]
    return out


@router.get("/quality/conversations/{cid}")
async def conversation_reviews(cid: int, agent: Agent = Depends(current_agent),
                               session: AsyncSession = Depends(get_session)):
    """Revisiones de una conversación (panel del inbox), la más reciente primero."""
    conv = await _conversation(session, agent.organization_id, cid)
    rows = (await session.scalars(select(ConversationReview).where(ConversationReview.conversation_id == conv.id)
                                  .order_by(ConversationReview.updated_at.desc()))).all()
    return {"conversation_id": conv.id, "qa_score": float(conv.qa_score) if conv.qa_score is not None else None,
            "sentiment": conv.ai_sentiment, "reviews": await reviews_out(session, list(rows))}


class ReReviewIn(BaseModel):
    scorecard_id: int | None = None


@router.post("/quality/conversations/{cid}/review")
async def rereview(cid: int, body: ReReviewIn, agent: Agent = Depends(require_supervisor),
                   session: AsyncSession = Depends(get_session)):
    """Revisa (o vuelve a revisar) con la IA ahora, aunque no esté en la muestra."""
    conv = await _conversation(session, agent.organization_id, cid)
    await ensure_defaults(session, agent.organization_id)
    if body.scorecard_id:
        await _scorecard(session, agent.organization_id, body.scorecard_id)
    done = await review_conversation(session, conv, auto=False, force=True, scorecard_id=body.scorecard_id)
    if not done:
        raise HTTPException(422, "Ninguna rúbrica aplica a esta conversación (¿hubo respuestas de un asesor o del bot?)")
    return await reviews_out(session, done)


class HumanReviewIn(BaseModel):
    scorecard_id: int
    scores: dict  # {criterio: {score, applies?, comment?, evidence?}}
    summary: str | None = None
    sentiment: str | None = None


@router.post("/quality/conversations/{cid}/human-review")
async def create_human_review(cid: int, body: HumanReviewIn, agent: Agent = Depends(require_supervisor),
                              session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, agent.organization_id, cid)
    sc = await _scorecard(session, agent.organization_id, body.scorecard_id)
    try:
        review = await human_review(session, conv, sc, agent, body.scores, body.summary, body.sentiment)
    except ReviewError as e:
        raise HTTPException(422, str(e)) from e
    return (await reviews_out(session, [review]))[0]


class DisputeIn(BaseModel):
    note: str


@router.post("/quality/reviews/{rid}/dispute")
async def dispute(rid: int, body: DisputeIn, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    """El asesor evaluado (o un supervisor) impugna la revisión."""
    r = await _review(session, agent.organization_id, rid)
    if r.agent_id != agent.id and not is_supervisor(agent):
        raise HTTPException(403, "Solo el asesor evaluado puede impugnar esta revisión")
    if r.status not in ("done", "disputed"):
        raise HTTPException(409, "Solo se pueden impugnar revisiones terminadas")
    if not body.note.strip():
        raise HTTPException(422, "Explica por qué no estás de acuerdo")
    r.status, r.dispute_note, r.updated_at = "disputed", body.note.strip()[:2000], utcnow()
    await session.commit()
    return (await reviews_out(session, [r]))[0]


class ResolveIn(BaseModel):
    action: str  # uphold | adjust
    scores: dict | None = None
    note: str | None = None


@router.post("/quality/reviews/{rid}/resolve")
async def resolve_dispute(rid: int, body: ResolveIn, agent: Agent = Depends(require_supervisor),
                          session: AsyncSession = Depends(get_session)):
    """Mantener la revisión o ajustar puntajes (se recalcula el total)."""
    r = await _review(session, agent.organization_id, rid)
    if r.status != "disputed":
        raise HTTPException(409, "La revisión no está impugnada")
    if body.action not in ("uphold", "adjust"):
        raise HTTPException(422, "action debe ser uphold o adjust")
    if body.action == "adjust":
        sc = await session.get(QAScorecard, r.scorecard_id) if r.scorecard_id else None
        if not sc or not body.scores:
            raise HTTPException(422, "Indica los puntajes a ajustar")
        from app.quality.reviews import normalize_scores

        merged = {**(r.scores or {}), **normalize_scores(sc.criteria, body.scores)}
        r.scores = merged
        r.total_score, r.critical_failed = score_review(sc.criteria, merged)
    verdict = "ajustada" if body.action == "adjust" else "mantenida"
    r.dispute_note = f"{r.dispute_note or ''}\n— Resolución ({verdict} por {agent.name}): {(body.note or '').strip()}"[:2000]
    r.status, r.updated_at = "done", utcnow()
    await session.flush()
    await finalize(session, await session.get(Conversation, r.conversation_id))
    await session.commit()
    return (await reviews_out(session, [r]))[0]


# --- Coaching -----------------------------------------------------------------------
def _target_agent(agent: Agent, agent_id: int | None) -> int:
    if agent_id and agent_id != agent.id and not is_supervisor(agent):
        raise HTTPException(403, "Solo puedes ver tu propio coaching")
    return agent_id or agent.id


@router.get("/quality/coaching")
async def list_coaching(agent_id: int | None = None, status: str | None = "open", limit: int = 100,
                        agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    target = _target_agent(agent, agent_id)
    q = select(CoachingItem).where(CoachingItem.organization_id == agent.organization_id,
                                   CoachingItem.agent_id == target)
    if status:
        q = q.where(CoachingItem.status == status)
    rows = (await session.scalars(q.order_by(CoachingItem.created_at.desc()).limit(max(1, min(limit, 500))))).all()
    return [coach.item_out(i) for i in rows]


class CoachingStatusIn(BaseModel):
    status: str


@router.patch("/quality/coaching/{item_id}")
async def update_coaching(item_id: int, body: CoachingStatusIn, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    item = await session.get(CoachingItem, item_id)
    if not item or item.organization_id != agent.organization_id:
        raise HTTPException(404, "Sugerencia no encontrada")
    if item.agent_id != agent.id and not is_supervisor(agent):
        raise HTTPException(403, "No es tu sugerencia")
    if body.status not in ("open", "acknowledged", "done", "dismissed"):
        raise HTTPException(422, "Estado inválido")
    item.status = body.status
    item.resolved_at = utcnow() if body.status in ("done", "dismissed") else None
    await session.commit()
    return coach.item_out(item)


@router.get("/quality/coaching/profile")
async def coaching_profile(agent_id: int | None = None, days: int = 30, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    target = _target_agent(agent, agent_id)
    other = await session.get(Agent, target)
    if not other or other.organization_id != agent.organization_id:
        raise HTTPException(404, "Asesor no encontrado")
    lo, hi = coach.default_range(max(1, min(days, 365)))
    return {**await coach.profile(session, agent.organization_id, target, lo, hi), "agent_name": other.name,
            "days": days}


# --- Reporte ------------------------------------------------------------------------
@router.get("/reports/qa")
async def qa_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(require_supervisor),
                    session: AsyncSession = Depends(get_session)):
    """Puntaje promedio por asesor y del bot, fallas críticas, sentimiento y tendencia (reporting.daily_qa)."""
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select day, subject_type, agent_id, reviews, score_sum::float as score_sum, critical_failed, positive,
               neutral, negative
        from reporting.daily_qa where organization_id = :o and day between :a and :b""", o=org, a=start, b=end)
    names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
    keys = ("reviews", "score_sum", "critical_failed", "positive", "neutral", "negative")
    by_subject: dict[tuple, Counter] = defaultdict(Counter)
    series: dict[str, Counter] = {d.isoformat(): Counter() for d in _days(start, end)}
    for r in rows:
        for k in keys:
            by_subject[(r["subject_type"], r["agent_id"])][k] += r[k] or 0
            series[r["day"].isoformat()][k] += r[k] or 0

    def summarize(c: Counter) -> dict:
        return {"reviews": c["reviews"], "avg_score": round(c["score_sum"] / c["reviews"], 1) if c["reviews"] else None,
                "critical_failed": c["critical_failed"], "critical_pct": _pct(c["critical_failed"], c["reviews"]),
                "positive": c["positive"], "neutral": c["neutral"], "negative": c["negative"],
                "negative_pct": _pct(c["negative"], c["reviews"])}

    agents = [{"agent_id": aid, "name": names.get(aid, "(sin asesor)"), **summarize(c)}
              for (subject, aid), c in by_subject.items() if subject == "agent"]
    agents.sort(key=lambda a: (a["avg_score"] is None, a["avg_score"] or 0))
    bot = Counter()
    for (subject, _aid), c in by_subject.items():
        if subject == "bot":
            bot.update(c)
    total = Counter()
    for c in by_subject.values():
        total.update(c)
    open_coaching = await session.scalar(select(func.count()).where(
        CoachingItem.organization_id == org, CoachingItem.status == "open")) or 0
    disputed = await session.scalar(select(func.count()).where(
        ConversationReview.organization_id == org, ConversationReview.status == "disputed")) or 0
    return {"start": start, "end": end, "totals": {**summarize(total), "open_coaching": open_coaching,
                                                   "disputed": disputed},
            "agents": agents, "bot": summarize(bot),
            "series": [{"day": d, "reviews": c["reviews"],
                        "avg_score": round(c["score_sum"] / c["reviews"], 1) if c["reviews"] else None,
                        "positive": c["positive"], "neutral": c["neutral"], "negative": c["negative"]}
                       for d, c in series.items()]}

