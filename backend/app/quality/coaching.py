"""Perfil de coaching de un asesor: promedio por criterio en un rango, tendencia y debilidades con ejemplos."""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CoachingItem, ConversationReview, QAScorecard


async def _reviews(session: AsyncSession, org: int, agent_id: int, lo: datetime, hi: datetime):
    return (await session.scalars(select(ConversationReview).where(
        ConversationReview.organization_id == org, ConversationReview.agent_id == agent_id,
        ConversationReview.subject_type == "agent", ConversationReview.status.in_(("done", "disputed")),
        ConversationReview.created_at >= lo, ConversationReview.created_at < hi))).all()


def _by_criterion(reviews) -> dict[str, list[tuple[int, ConversationReview]]]:
    out: dict[str, list] = defaultdict(list)
    for r in reviews:
        for key, s in (r.scores or {}).items():
            if s.get("applies", True) and s.get("score") is not None:
                out[key].append((int(s["score"]), r))
    return out


def _avg(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 1) if values else None


async def profile(session: AsyncSession, org: int, agent_id: int, lo: datetime, hi: datetime) -> dict:
    now_reviews = await _reviews(session, org, agent_id, lo, hi)
    prev_reviews = await _reviews(session, org, agent_id, lo - (hi - lo), lo)
    labels: dict[str, str] = {}
    for sc in (await session.scalars(select(QAScorecard).where(QAScorecard.organization_id == org))).all():
        for c in sc.criteria or []:
            labels.setdefault(c["key"], c["label"])
    cur, prev = _by_criterion(now_reviews), _by_criterion(prev_reviews)
    criteria = []
    for key, items in cur.items():
        avg = _avg(s for s, _ in items)
        prev_avg = _avg(s for s, _ in prev.get(key, []))
        worst = sorted(items, key=lambda x: x[0])[:2]
        criteria.append({
            "key": key, "label": labels.get(key, key), "avg": avg, "samples": len(items), "prev_avg": prev_avg,
            "trend": round(avg - prev_avg, 1) if avg is not None and prev_avg is not None else None,
            "examples": [{"review_id": r.id, "conversation_id": r.conversation_id, "score": s,
                          "evidence": (r.scores.get(key) or {}).get("evidence"),
                          "comment": (r.scores.get(key) or {}).get("comment")} for s, r in worst],
        })
    criteria.sort(key=lambda c: c["avg"] if c["avg"] is not None else 101)
    open_items = (await session.scalars(select(CoachingItem).where(
        CoachingItem.organization_id == org, CoachingItem.agent_id == agent_id, CoachingItem.status == "open")
        .order_by(CoachingItem.created_at.desc()).limit(20))).all()
    totals = [float(r.total_score) for r in now_reviews if r.total_score is not None]
    prev_totals = [float(r.total_score) for r in prev_reviews if r.total_score is not None]
    avg_total, prev_total = _avg(totals), _avg(prev_totals)
    return {
        "agent_id": agent_id, "reviews": len(now_reviews), "avg_score": avg_total, "prev_avg_score": prev_total,
        "trend": round(avg_total - prev_total, 1) if avg_total is not None and prev_total is not None else None,
        "critical_failed": sum(1 for r in now_reviews if r.critical_failed),
        "criteria": criteria,
        "weaknesses": [c for c in criteria if c["avg"] is not None and c["avg"] < 80][:3],
        "open_items": [item_out(i) for i in open_items],
    }


def item_out(i: CoachingItem) -> dict:
    return {"id": i.id, "agent_id": i.agent_id, "review_id": i.review_id, "criterion_key": i.criterion_key,
            "title": i.title, "suggestion": i.suggestion, "example": i.example, "status": i.status,
            "created_at": i.created_at, "resolved_at": i.resolved_at}


def default_range(days: int = 30) -> tuple[datetime, datetime]:
    from app.models import utcnow

    hi = utcnow() + timedelta(seconds=1)
    return hi - timedelta(days=days), hi
