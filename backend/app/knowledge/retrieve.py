"""Recuperación con citas para el agente de IA, el copiloto y el asistente.

1. Embedding de la pregunta (input_type=query) → `public.knowledge_search` (vector + texto en español, RRF;
   solo documentos listos, activos y vigentes). Sin proveedor de embeddings: solo la parte de texto.
2. Filtros: fuentes disponibles para el agente de IA (`knowledge_sources.ai_agent_ids` vacío = todos) y, en los
   documentos anteriores, los agentes a los que estaban conectados.
3. Rerank opcional (Voyage `rerank-2.5`, solo si se configuró una conexión de rerank).
4. Cada consulta queda en `knowledge_queries` (resultados, mejor puntaje, latencia).

Respondida o no — opción robusta elegida: los fragmentos llegan al modelo etiquetados [S1]…[Sn] y el modelo
termina su respuesta con una línea `[[fuentes: S1,S3]]` o `[[fuentes: ninguna]]`. `finish()` quita esa línea (y
cualquier [Sn] en el texto) ANTES de enviar al cliente y marca `answered`. No agrega llamadas ni herramientas; si
el modelo omite la etiqueta, `answered` queda desconocido (null) salvo que haya citado [Sn] en el texto.
Una consulta sin resultados o con puntaje bajo cuenta como sin respuesta de inmediato y alimenta los vacíos.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import embeddings
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeQuery, KnowledgeSource

log = logging.getLogger(__name__)

FOOTER_RE = re.compile(r"\[\[\s*fuentes?\s*:\s*([^\]]*)\]\]", re.I)
INLINE_RE = re.compile(r"\s?\[S(\d{1,2})\]")
MIN_QUERY_CHARS = 4


@dataclass
class Passage:
    label: str
    chunk_id: int
    document_id: int
    source_id: int
    source_type: str
    title: str
    uri: str | None
    heading: str | None
    content: str
    score: float

    def citation(self) -> dict:
        return {"label": self.label, "document_id": self.document_id, "chunk_id": self.chunk_id, "title": self.title,
                "uri": self.uri, "heading": self.heading, "score": round(self.score, 4), "source": self.source_type}


@dataclass
class Retrieval:
    organization_id: int
    query: str
    passages: list[Passage] = field(default_factory=list)
    top_score: float | None = None
    query_id: int | None = None
    query_created_at: object = None
    embedding: list[float] | None = None
    latency_ms: int = 0
    answered: bool | None = None

    def prompt_block(self, max_chars: int = 6000, *, footer: bool = True) -> str:
        """Fragmentos etiquetados para el prompt (presupuesto en caracteres)."""
        if not self.passages:
            return ""
        lines, used = [], 0
        for p in self.passages:
            head = f"[{p.label}] {p.title}" + (f" › {p.heading}" if p.heading and p.heading != p.title else "")
            body = p.content.strip()
            room = max_chars - used - len(head) - 4
            if room < 200:
                break
            body = body[:room]
            lines.append(f"{head}\n{body}")
            used += len(head) + len(body) + 4
        block = ("## Base de conocimiento (fragmentos recuperados para este mensaje)\n"
                 "Para datos de la empresa (productos, precios, políticas, horarios, procesos) usa SOLO estos "
                 "fragmentos. Si la respuesta no está aquí, di con amabilidad «no tengo esa información» y ofrece "
                 "pasar con un asesor; nunca inventes.\n\n" + "\n\n".join(lines))
        if footer:
            block += ("\n\nAl final de tu respuesta agrega una línea aparte exactamente así: [[fuentes: S1,S2]] con "
                      "las etiquetas de los fragmentos que usaste, o [[fuentes: ninguna]] si no usaste ninguno. Esa "
                      "línea se elimina antes de enviar al cliente; no pongas etiquetas [S1] dentro del texto.")
        return block

    def citations(self) -> list[dict]:
        return [p.citation() for p in self.passages]


def _vector_literal(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.7g}" for x in v) + "]"


async def _allowed_sources(session: AsyncSession, org: int, ai_agent_id: int | None,
                           source_ids: list[int] | None) -> list[int] | None:
    if ai_agent_id is None and not source_ids:
        return None
    q = select(KnowledgeSource.id, KnowledgeSource.ai_agent_ids).where(
        KnowledgeSource.organization_id == org, KnowledgeSource.is_active)
    if source_ids:
        q = q.where(KnowledgeSource.id.in_(source_ids))
    rows = (await session.execute(q)).all()
    return [sid for sid, agents in rows if ai_agent_id is None or not agents or ai_agent_id in agents] or [0]


async def search(session: AsyncSession, org: int, query: str, *, limit: int | None = None,
                 ai_agent_id: int | None = None, source_ids: list[int] | None = None, conversation_id: int | None = None,
                 agent_id: int | None = None, log_query: bool = True, rerank: bool = True) -> Retrieval:
    from app.settings_store import get_setting

    started = time.monotonic()
    cfg = await get_setting(session, "knowledge", org)
    limit = int(limit or cfg.get("top_k") or 6)
    query = (query or "").strip()[:2000]
    out = Retrieval(organization_id=org, query=query)
    if len(query) < MIN_QUERY_CHARS or not cfg.get("enabled", True):
        return out

    emb = await embeddings.get_embedder(session, org)
    if emb is not None:
        try:
            out.embedding = (await embeddings.embed(emb, org, [query], "query")).vectors[0]
        except embeddings.EmbeddingError as e:
            log.warning("Embedding de la consulta falló (sigue solo texto): %s", e)
    allowed = await _allowed_sources(session, org, ai_agent_id, source_ids)
    rows = (await session.execute(text(
        "select chunk_id, document_id, score from public.knowledge_search("
        ":o, cast(cast(:e as text) as extensions.vector), :q, :l, cast(:s as bigint[]))"),
        {"o": org, "e": _vector_literal(out.embedding), "q": query, "l": limit * 2 if rerank else limit,
         "s": allowed})).all()
    if rows:
        ids = [r.chunk_id for r in rows]
        found = {c.id: c for c in (await session.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.document_id, KnowledgeChunk.heading, KnowledgeChunk.content)
            .where(KnowledgeChunk.id.in_(ids)))).all()}
        docs = {d.id: d for d in (await session.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.id.in_({r.document_id for r in rows})))).all()}
        srcs = {s.id: s for s in (await session.scalars(select(KnowledgeSource).where(
            KnowledgeSource.id.in_({d.source_id for d in docs.values()})))).all()}
        passages = []
        for r in rows:
            c, d = found.get(r.chunk_id), docs.get(r.document_id)
            if not c or not d:
                continue
            s = srcs.get(d.source_id)
            legacy_agents = (d.metadata_ or {}).get("ai_agent_ids") if s and s.type == "legacy_docs" else None
            if ai_agent_id is not None and legacy_agents is not None and ai_agent_id not in legacy_agents:
                continue  # documento anterior conectado solo a otros agentes
            passages.append(Passage("", c.id, d.id, d.source_id, s.type if s else "", d.title, d.uri, c.heading,
                                    c.content, float(r.score)))
        if rerank and len(passages) > 1:
            rr = await embeddings.get_embedder(session, org, rerank=True)
            if rr is not None:
                try:
                    order = await rr.rerank(query, [p.content for p in passages], limit)
                    if order:
                        passages = [passages[i] for i, _ in order if i < len(passages)]
                except embeddings.EmbeddingError as e:
                    log.warning("Rerank falló (se usa el orden híbrido): %s", e)
        passages = passages[:limit]
        for i, p in enumerate(passages, start=1):
            p.label = f"S{i}"
        out.passages = passages
        out.top_score = max((float(r.score) for r in rows), default=None)
    out.latency_ms = int((time.monotonic() - started) * 1000)
    if not out.passages or (out.top_score or 0) < float(cfg.get("min_score") or 0):
        out.answered = False
    if log_query:
        await _log_query(session, out, conversation_id, ai_agent_id, agent_id)
        if out.answered is False:
            from app.knowledge import gaps

            await gaps.record(session, org, query, out.embedding)
    return out


async def _log_query(session: AsyncSession, r: Retrieval, conversation_id, ai_agent_id, agent_id) -> None:
    q = KnowledgeQuery(organization_id=r.organization_id, conversation_id=conversation_id, ai_agent_id=ai_agent_id,
                       agent_id=agent_id, query=r.query,
                       results=[{"chunk_id": p.chunk_id, "document_id": p.document_id, "score": round(p.score, 5)}
                                for p in r.passages],
                       top_score=r.top_score, answered=r.answered, latency_ms=r.latency_ms)
    session.add(q)
    await session.flush()
    r.query_id, r.query_created_at = q.id, q.created_at


def strip_citations(reply: str) -> tuple[str, list[str] | None]:
    """(texto limpio para el cliente, etiquetas usadas | None si el modelo no dijo nada)."""
    used: list[str] | None = None
    for m in FOOTER_RE.finditer(reply or ""):
        raw = m.group(1).strip().lower()
        labels = [] if raw in ("", "ninguna", "ninguno", "none") else \
            [f"S{n}" for n in re.findall(r"s?\s*(\d{1,2})", raw)]
        used = (used or []) + labels
    inline = [f"S{n}" for n in INLINE_RE.findall(reply or "")]
    if inline:
        used = sorted(set((used or []) + inline))
    cleaned = INLINE_RE.sub("", FOOTER_RE.sub("", reply or ""))
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), used


async def finish(session: AsyncSession, r: Retrieval | None, reply: str | None) -> str | None:
    """Quita la etiqueta de fuentes y marca la consulta (respondida / sin respuesta → vacío)."""
    if not reply:
        return reply
    cleaned, used = strip_citations(reply)
    if r is None or r.query_id is None:
        return cleaned
    valid = {p.label for p in r.passages}
    if used is not None:
        answered = bool([u for u in used if u in valid])
        if r.answered is not False:  # una consulta ya marcada sin resultados no cambia
            await session.execute(text(
                "update public.knowledge_queries set answered = :a, results = cast(:res as jsonb) "
                "where id = :i and created_at = :c"),
                {"a": answered, "i": r.query_id, "c": r.query_created_at,
                 "res": json.dumps([{**c, "used": c["label"] in used} for c in
                                    [{"chunk_id": p.chunk_id, "document_id": p.document_id,
                                      "score": round(p.score, 5), "label": p.label} for p in r.passages]])})
            if not answered:
                from app.knowledge import gaps

                await gaps.record(session, r.organization_id, r.query, r.embedding)
            r.answered = answered
    return cleaned


def is_question(query: str) -> bool:
    """Saludos y mensajes muy cortos no se buscan (no ensucian consultas ni vacíos)."""
    q = (query or "").strip()
    return len(q) >= MIN_QUERY_CHARS and ("?" in q or len(q.split()) >= 3)


async def has_index(session: AsyncSession, org: int) -> bool:
    return bool(await session.scalar(select(KnowledgeDocument.id).where(
        KnowledgeDocument.organization_id == org, KnowledgeDocument.status == "ready").limit(1)))


async def for_agent(session: AsyncSession, org: int, history: list, *, ai_agent_id: int,
                    conversation_id: int) -> Retrieval | None:
    """Recuperación para el agente de IA con los últimos mensajes entrantes del cliente (seguido de una ráfaga).
    Nunca rompe la respuesta: ante cualquier error devuelve None (el agente responde sin RAG)."""
    from app.settings_store import get_setting

    parts = []
    for m in reversed(history):
        if m.direction != "in":
            break
        parts.append((m.text or m.transcript or "").strip())
    query = " ".join(reversed([p for p in parts if p]))[-1000:]
    if not is_question(query):
        return None
    try:
        if not (await get_setting(session, "knowledge", org)).get("enabled", True) or not await has_index(session, org):
            return None
        async with session.begin_nested():
            return await search(session, org, query, ai_agent_id=ai_agent_id, conversation_id=conversation_id)
    except Exception:  # noqa: BLE001
        log.warning("Búsqueda en la base de conocimiento falló (conversación %s)", conversation_id, exc_info=True)
        return None


async def max_context_chars(session: AsyncSession, org: int) -> int:
    from app.settings_store import get_setting

    return int((await get_setting(session, "knowledge", org)).get("max_context_chars") or 6000)
