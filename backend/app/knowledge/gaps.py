"""Vacíos de conocimiento: preguntas que la base no pudo responder, agrupadas por similitud.

Una pregunta sin respuesta se une al vacío abierto más parecido (coseno ≥ `knowledge.gap_similarity`, 0.85 por
defecto; sin embeddings: misma pregunta normalizada) o abre uno nuevo. «Responder» un vacío crea un documento de
preguntas frecuentes en la fuente FAQ de la empresa y lo marca respondido.
"""

import re
import unicodedata

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeGap, utcnow

MAX_EXAMPLES = 10


def _norm(q: str) -> str:
    s = unicodedata.normalize("NFKD", (q or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", s))


async def record(session: AsyncSession, org: int, question: str, embedding: list[float] | None) -> KnowledgeGap:
    from app.settings_store import get_setting

    question = (question or "").strip()[:500]
    threshold = float((await get_setting(session, "knowledge", org)).get("gap_similarity") or 0.85)
    gap = None
    if embedding is not None:
        row = (await session.execute(text(
            "select id, 1 - (embedding operator(extensions.<=>) cast(cast(:e as text) as extensions.vector)) as sim from public.knowledge_gaps "
            "where organization_id = :o and status = 'open' and embedding is not null "
            "order by embedding operator(extensions.<=>) cast(cast(:e as text) as extensions.vector) limit 1"),
            {"o": org, "e": "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"})).first()
        if row and float(row.sim) >= threshold:
            gap = await session.get(KnowledgeGap, row.id)
    if gap is None:
        target = _norm(question)
        for g in (await session.scalars(select(KnowledgeGap).where(
                KnowledgeGap.organization_id == org, KnowledgeGap.status == "open")
                .order_by(KnowledgeGap.last_seen_at.desc()).limit(200))).all():
            if _norm(g.topic) == target:
                gap = g
                break
    if gap is None:
        gap = KnowledgeGap(organization_id=org, topic=question, examples=[question], occurrences=1,
                           embedding=embedding)
        session.add(gap)
    else:
        gap.occurrences = (gap.occurrences or 0) + 1
        gap.last_seen_at = utcnow()
        examples = list(gap.examples or [])
        if question not in examples and len(examples) < MAX_EXAMPLES:
            gap.examples = [*examples, question]
    await session.flush()
    return gap


async def faq_source(session: AsyncSession, org: int):
    """Fuente FAQ de la empresa (la crea la primera respuesta a un vacío)."""
    from app.models import KnowledgeSource

    src = await session.scalar(select(KnowledgeSource).where(
        KnowledgeSource.organization_id == org, KnowledgeSource.type == "faq").order_by(KnowledgeSource.id).limit(1))
    if src is None:
        src = KnowledgeSource(organization_id=org, type="faq", name="Preguntas frecuentes", config={"items": []})
        session.add(src)
        await session.flush()
    return src


async def answer(session: AsyncSession, gap: KnowledgeGap, answer_text: str, question: str | None = None):
    """«Responder»: agrega la pregunta y respuesta a la FAQ, la indexa y cierra el vacío."""
    from app.knowledge.ingest import refresh_counts, upsert_document
    from app.knowledge.text import checksum

    q = (question or gap.topic).strip()[:500]
    src = await faq_source(session, gap.organization_id)
    items = [i for i in (src.config or {}).get("items") or []
             if (i.get("question") or "").strip().lower() != q.lower()]
    src.config = {**(src.config or {}), "items": [*items, {"question": q, "answer": answer_text.strip()}]}
    doc, _ = await upsert_document(session, src, uri="faq:" + checksum(q.lower())[:16], title=q[:200],
                                   text=f"Pregunta: {q}\n\nRespuesta: {answer_text.strip()}",
                                   metadata={"gap_id": gap.id, "examples": list(gap.examples or [])[:5]})
    await refresh_counts(session, src)
    gap.status, gap.resolved_document_id = "answered", doc.id
    await session.flush()
    return doc


async def ignore(session: AsyncSession, gap: KnowledgeGap) -> None:
    gap.status = "ignored"
    await session.flush()
