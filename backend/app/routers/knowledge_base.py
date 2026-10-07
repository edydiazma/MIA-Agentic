"""Base de conocimiento con RAG (/api/knowledge-base): fuentes, documentos, fragmentos, búsqueda de prueba, vacíos
y uso. La base anterior (/api/knowledge) sigue funcionando y aparece aquí como la fuente «Documentos anteriores»."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import current_agent, require_admin
from app.db import get_session
from app.knowledge import gaps, ingest, parsers, retrieve
from app.models import (
    Agent,
    AICall,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeGap,
    KnowledgeQuery,
    KnowledgeSource,
    utcnow,
)
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/knowledge-base", tags=["knowledge-base"])

EDITABLE_TYPES = ("upload", "website", "catalog", "conversations", "faq", "api")


# --- Esquemas --------------------------------------------------------------------------------------------------
class SourceIn(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=200)
    config: dict = {}
    refresh_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    ai_agent_ids: list[int] = []
    is_active: bool = True


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    refresh_hours: int | None = Field(default=None, ge=0, le=24 * 30)  # 0 = manual
    ai_agent_ids: list[int] | None = None
    is_active: bool | None = None


class SourceOut(BaseModel):
    id: int
    type: str
    name: str
    config: dict
    status: str
    refresh_hours: int | None
    documents_count: int
    chunks_count: int
    last_synced_at: UTCDateTime | None
    last_error: str | None
    ai_agent_ids: list[int]
    is_active: bool
    created_at: UTCDateTime


class DocumentOut(BaseModel):
    id: int
    source_id: int
    title: str
    uri: str | None
    mime: str | None
    language: str | None
    status: str
    chunks_count: int
    tokens: int | None
    valid_until: UTCDateTime | None
    error: str | None
    metadata: dict
    updated_at: UTCDateTime


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=2_000_000)
    uri: str | None = Field(default=None, max_length=1000)
    valid_until: datetime | None = None
    metadata: dict = {}


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    valid_until: datetime | None = None
    clear_valid_until: bool = False
    excluded: bool | None = None


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    ai_agent_id: int | None = None
    source_ids: list[int] | None = None
    limit: int = Field(default=6, ge=1, le=20)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)
    question: str | None = Field(default=None, max_length=500)


def _src(s: KnowledgeSource) -> SourceOut:
    return SourceOut(id=s.id, type=s.type, name=s.name, config=s.config or {}, status=s.status,
                     refresh_hours=s.refresh_hours, documents_count=s.documents_count, chunks_count=s.chunks_count,
                     last_synced_at=s.last_synced_at, last_error=s.last_error, ai_agent_ids=list(s.ai_agent_ids or []),
                     is_active=s.is_active, created_at=s.created_at)


def _doc(d: KnowledgeDocument) -> DocumentOut:
    return DocumentOut(id=d.id, source_id=d.source_id, title=d.title, uri=d.uri, mime=d.mime, language=d.language,
                       status=d.status, chunks_count=d.chunks_count, tokens=d.tokens, valid_until=d.valid_until,
                       error=d.error, metadata=d.metadata_ or {}, updated_at=d.updated_at)


def _validate_config(type_: str, config: dict) -> dict:
    config = dict(config or {})
    if type_ == "website":
        url = (config.get("url") or "").strip()
        if not url:
            raise HTTPException(422, "Indica la URL del sitio")
        from app.onboarding import website

        try:
            config["url"] = website.normalize_url(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"URL inválida: {e}") from e
        config["max_pages"] = max(1, min(int(config.get("max_pages") or ingest.DEFAULT_MAX_PAGES),
                                         ingest.MAX_PAGES_CAP))
    if type_ == "faq":
        items = config.get("items") or []
        if not isinstance(items, list):
            raise HTTPException(422, "items debe ser una lista de {question, answer}")
        config["items"] = [{"question": str(i.get("question") or "").strip()[:500],
                            "answer": str(i.get("answer") or "").strip()[:20_000]}
                           for i in items if isinstance(i, dict) and str(i.get("question") or "").strip()]
    return config


async def _source(session: AsyncSession, agent: Agent, sid: int) -> KnowledgeSource:
    s = await session.get(KnowledgeSource, sid)
    if not s or s.organization_id != agent.organization_id:
        raise HTTPException(404, "Fuente no encontrada")
    return s


async def _document(session: AsyncSession, agent: Agent, did: int) -> KnowledgeDocument:
    d = await session.get(KnowledgeDocument, did)
    if not d or d.organization_id != agent.organization_id:
        raise HTTPException(404, "Documento no encontrado")
    return d


# --- Fuentes ---------------------------------------------------------------------------------------------------
@router.get("/sources", response_model=list[SourceOut])
async def list_sources(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    legacy = await ingest.ensure_legacy_source(session, agent.organization_id)
    if legacy is not None and legacy.last_synced_at is None and legacy.status == "idle":
        await ingest.schedule_sync(session, legacy)
    await session.commit()
    rows = (await session.scalars(select(KnowledgeSource).where(
        KnowledgeSource.organization_id == agent.organization_id).order_by(KnowledgeSource.id))).all()
    return [_src(s) for s in rows]


@router.post("/sources", response_model=SourceOut)
async def create_source(body: SourceIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    if body.type not in EDITABLE_TYPES:
        raise HTTPException(422, f"Tipo de fuente inválido: {body.type}")
    s = KnowledgeSource(organization_id=agent.organization_id, type=body.type, name=body.name.strip(),
                        config=_validate_config(body.type, body.config), refresh_hours=body.refresh_hours,
                        ai_agent_ids=body.ai_agent_ids, is_active=body.is_active, created_by=agent.id)
    session.add(s)
    await session.flush()
    if body.type in ("website", "catalog", "conversations", "faq"):
        await ingest.schedule_sync(session, s)
    await session.commit()
    return _src(s)


@router.get("/sources/{sid}", response_model=SourceOut)
async def get_source(sid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return _src(await _source(session, agent, sid))


@router.patch("/sources/{sid}", response_model=SourceOut)
async def update_source(sid: int, body: SourceUpdate, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    s = await _source(session, agent, sid)
    resync = False
    if body.name is not None:
        s.name = body.name.strip()
    if body.config is not None and s.type != "legacy_docs":
        s.config = _validate_config(s.type, body.config)
        resync = s.type in ("website", "conversations", "faq")
    if body.refresh_hours is not None:
        s.refresh_hours = body.refresh_hours or None
    if body.ai_agent_ids is not None:
        s.ai_agent_ids = body.ai_agent_ids
    if body.is_active is not None:
        s.is_active = body.is_active
    s.updated_at = utcnow()
    if resync and s.is_active:
        await ingest.schedule_sync(session, s)
    await session.commit()
    return _src(s)


@router.delete("/sources/{sid}")
async def delete_source(sid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    s = await _source(session, agent, sid)
    if s.type == "legacy_docs":
        raise HTTPException(409, "Los documentos anteriores se administran en la base de conocimiento anterior; "
                                 "puedes desactivar la fuente")
    paths = (await session.scalars(select(KnowledgeDocument.storage_path).where(
        KnowledgeDocument.source_id == s.id, KnowledgeDocument.storage_path.is_not(None)))).all()
    await session.delete(s)
    await session.commit()
    for p in paths:
        try:
            await storage.remove(p)
        except Exception:  # noqa: BLE001 (el archivo huérfano no bloquea el borrado)
            pass
    return {"ok": True}


@router.post("/sources/{sid}/sync")
async def sync_now(sid: int, force: bool = False, agent: Agent = Depends(require_admin),
                   session: AsyncSession = Depends(get_session)):
    s = await _source(session, agent, sid)
    if not s.is_active:
        raise HTTPException(409, "La fuente está desactivada")
    await ingest.schedule_sync(session, s, force=force)
    await session.commit()
    return {"queued": True}


# --- Documentos ------------------------------------------------------------------------------------------------
@router.post("/sources/{sid}/upload", response_model=list[DocumentOut])
async def upload(sid: int, files: list[UploadFile] = File(...), valid_until: datetime | None = Form(None),
                 agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    s = await _source(session, agent, sid)
    if s.type != "upload":
        raise HTTPException(409, "Solo las fuentes de archivos aceptan archivos")
    out = []
    for f in files:
        name = f.filename or "archivo"
        ext = parsers.extension(name)
        if ext not in parsers.SUPPORTED:
            raise HTTPException(422, f"{name}: formato no soportado (PDF, Word, Excel, CSV, TXT, Markdown o HTML)")
        data = await f.read()
        if len(data) > parsers.MAX_FILE_BYTES:
            raise HTTPException(413, f"{name}: supera {parsers.MAX_FILE_BYTES // (1024 * 1024)} MB")
        mime = parsers.SUPPORTED[ext]
        path = await storage.upload(storage.new_path(storage.RESOURCES_BUCKET, agent.organization_id, "knowledge",
                                                     mime), data, mime)
        old = await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source_id == s.id,
                                                                  KnowledgeDocument.uri == f"file:{name}"))
        if old is not None and old.storage_path and old.storage_path != path:
            try:
                await storage.remove(old.storage_path)
            except Exception:  # noqa: BLE001
                pass
        doc = old or KnowledgeDocument(organization_id=agent.organization_id, source_id=s.id, uri=f"file:{name}",
                                       title=name[:500], metadata_={})
        doc.storage_path, doc.mime, doc.status, doc.error = path, mime, "pending", None
        doc.metadata_ = {**(doc.metadata_ or {}), "filename": name, "size": len(data), "uploaded_by": agent.id}
        if valid_until is not None:
            doc.valid_until = valid_until
        if old is None:
            session.add(doc)
        await session.flush()
        await ingest.schedule_index(session, doc)
        out.append(_doc(doc))
    await session.commit()
    return out


@router.post("/sources/{sid}/documents", response_model=DocumentOut)
async def push_document(sid: int, body: DocumentIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    """Documento enviado por API (fuentes «api»; también sirve para pegar texto en una fuente de archivos)."""
    s = await _source(session, agent, sid)
    if s.type not in ("api", "upload"):
        raise HTTPException(409, "Esta fuente se llena sola (sitio, catálogo, conversaciones o FAQ)")
    try:
        doc, _ = await ingest.upsert_document(
            session, s, uri=body.uri or f"api:{ingest.checksum(body.title)[:16]}", title=body.title,
            text=body.content, mime="text/plain", metadata=body.metadata, valid_until=body.valid_until)
    except ingest.embeddings.EmbeddingError as e:
        raise HTTPException(502, f"Embeddings: {e}") from e
    await ingest.refresh_counts(session, s)
    await session.commit()
    return _doc(doc)


@router.get("/documents")
async def list_documents(source_id: int | None = None, status: str | None = None, q: str | None = None,
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    cond = [KnowledgeDocument.organization_id == agent.organization_id]
    if source_id:
        cond.append(KnowledgeDocument.source_id == source_id)
    if status:
        cond.append(KnowledgeDocument.status == status)
    if q:
        cond.append(KnowledgeDocument.title.ilike(f"%{q.strip()}%"))
    total = await session.scalar(select(func.count()).select_from(KnowledgeDocument).where(*cond))
    rows = (await session.scalars(select(KnowledgeDocument).where(*cond)
                                  .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc())
                                  .limit(limit).offset(offset))).all()
    return {"total": total or 0, "items": [_doc(d) for d in rows]}


@router.get("/documents/{did}", response_model=DocumentOut)
async def get_document(did: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return _doc(await _document(session, agent, did))


@router.get("/documents/{did}/chunks")
async def document_chunks(did: int, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    d = await _document(session, agent, did)
    rows = (await session.execute(select(
        KnowledgeChunk.id, KnowledgeChunk.ordinal, KnowledgeChunk.heading, KnowledgeChunk.content,
        KnowledgeChunk.tokens, KnowledgeChunk.embedding_model).where(KnowledgeChunk.document_id == d.id)
        .order_by(KnowledgeChunk.ordinal))).all()
    return [{"id": r.id, "ordinal": r.ordinal, "heading": r.heading, "content": r.content, "tokens": r.tokens,
             "embedded": r.embedding_model is not None, "embedding_model": r.embedding_model} for r in rows]


@router.patch("/documents/{did}", response_model=DocumentOut)
async def update_document(did: int, body: DocumentUpdate, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    d = await _document(session, agent, did)
    if body.title is not None:
        d.title = body.title.strip()
    if body.clear_valid_until:
        d.valid_until = None
    elif body.valid_until is not None:
        d.valid_until = body.valid_until
    if body.excluded is True:
        d.status = "excluded"
    elif body.excluded is False and d.status == "excluded":
        d.status, d.checksum = "pending", None
        if d.storage_path:
            await ingest.schedule_index(session, d)
        else:
            s = await session.get(KnowledgeSource, d.source_id)
            await ingest.schedule_sync(session, s)
    d.updated_at = utcnow()
    s = await session.get(KnowledgeSource, d.source_id)
    await ingest.refresh_counts(session, s)
    await session.commit()
    return _doc(d)


@router.post("/documents/{did}/reindex")
async def reindex_document(did: int, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    d = await _document(session, agent, did)
    if d.storage_path:
        d.status = "pending"
        await ingest.schedule_index(session, d)
    else:
        await ingest.schedule_sync(session, await session.get(KnowledgeSource, d.source_id), force=True)
    await session.commit()
    return {"queued": True}


@router.delete("/documents/{did}")
async def delete_document(did: int, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    d = await _document(session, agent, did)
    s = await session.get(KnowledgeSource, d.source_id)
    if s.type not in ("upload", "api"):
        raise HTTPException(409, "Este documento viene de una fuente sincronizada: exclúyelo en lugar de borrarlo")
    path = d.storage_path
    await session.delete(d)
    await session.flush()
    await ingest.refresh_counts(session, s)
    await session.commit()
    if path:
        try:
            await storage.remove(path)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


# --- Búsqueda de prueba («Probar búsqueda») ----------------------------------------------------------------------
@router.post("/search")
async def search(body: SearchIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if body.source_ids:
        own = set((await session.scalars(select(KnowledgeSource.id).where(
            KnowledgeSource.organization_id == agent.organization_id,
            KnowledgeSource.id.in_(body.source_ids)))).all())
        if own != set(body.source_ids):
            raise HTTPException(404, "Fuente no encontrada")
    r = await retrieve.search(session, agent.organization_id, body.query, limit=body.limit,
                              ai_agent_id=body.ai_agent_id, source_ids=body.source_ids, agent_id=agent.id,
                              log_query=False)
    return {"query": r.query, "latency_ms": r.latency_ms, "top_score": r.top_score, "semantic": r.embedding is not None,
            "passages": [{**p.citation(), "content": p.content} for p in r.passages],
            "prompt": r.prompt_block(footer=False)}


# --- Vacíos ----------------------------------------------------------------------------------------------------
@router.get("/gaps")
async def list_gaps(status: str = "open", limit: int = Query(100, ge=1, le=500), agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(KnowledgeGap).where(
        KnowledgeGap.organization_id == agent.organization_id, KnowledgeGap.status == status)
        .order_by(KnowledgeGap.occurrences.desc(), KnowledgeGap.last_seen_at.desc()).limit(limit))).all()
    return [{"id": g.id, "topic": g.topic, "examples": list(g.examples or []), "occurrences": g.occurrences,
             "first_seen_at": g.first_seen_at, "last_seen_at": g.last_seen_at, "status": g.status,
             "resolved_document_id": g.resolved_document_id} for g in rows]


async def _gap(session: AsyncSession, agent: Agent, gid: int) -> KnowledgeGap:
    g = await session.get(KnowledgeGap, gid)
    if not g or g.organization_id != agent.organization_id:
        raise HTTPException(404, "Vacío no encontrado")
    return g


@router.post("/gaps/{gid}/answer")
async def answer_gap(gid: int, body: AnswerIn, agent: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    g = await _gap(session, agent, gid)
    doc = await gaps.answer(session, g, body.answer, body.question)
    await session.commit()
    return {"ok": True, "document": _doc(doc)}


@router.post("/gaps/{gid}/ignore")
async def ignore_gap(gid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await gaps.ignore(session, await _gap(session, agent, gid))
    await session.commit()
    return {"ok": True}


# --- Uso -------------------------------------------------------------------------------------------------------
@router.get("/stats")
async def stats(days: int = Query(30, ge=1, le=180), agent: Agent = Depends(current_agent),
                session: AsyncSession = Depends(get_session)):
    org, since = agent.organization_id, utcnow() - timedelta(days=days)
    q = (await session.execute(select(
        func.count(), func.count().filter(KnowledgeQuery.answered.is_(True)),
        func.count().filter(KnowledgeQuery.answered.is_(False)), func.avg(KnowledgeQuery.latency_ms))
        .where(KnowledgeQuery.organization_id == org, KnowledgeQuery.created_at >= since))).one()
    docs = (await session.execute(select(
        func.count(), func.coalesce(func.sum(KnowledgeDocument.chunks_count), 0),
        func.count().filter(KnowledgeDocument.status == "failed"))
        .where(KnowledgeDocument.organization_id == org, KnowledgeDocument.status != "excluded"))).one()
    cost = (await session.execute(select(func.coalesce(func.sum(AICall.cost_usd), 0),
                                         func.coalesce(func.sum(AICall.input_tokens), 0)).where(
        AICall.organization_id == org, AICall.purpose == "embedding", AICall.created_at >= since))).one()
    open_gaps = await session.scalar(select(func.count()).select_from(KnowledgeGap).where(
        KnowledgeGap.organization_id == org, KnowledgeGap.status == "open"))
    total, answered, unanswered = int(q[0]), int(q[1]), int(q[2])
    return {"days": days, "queries": total, "answered": answered, "unanswered": unanswered,
            "answer_rate": round(answered / (answered + unanswered), 4) if answered + unanswered else None,
            "avg_latency_ms": round(float(q[3])) if q[3] is not None else None,
            "documents": int(docs[0]), "chunks": int(docs[1]), "failed_documents": int(docs[2]),
            "open_gaps": open_gaps or 0, "embedding_cost_usd": round(float(cost[0]), 6),
            "embedding_tokens": int(cost[1])}
