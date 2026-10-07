"""Ingesta de la base de conocimiento: fuentes → documentos → fragmentos con embedding.

Tipos de fuente (knowledge_sources.type):
- upload: archivos subidos (PDF, Word, Excel, CSV, TXT/MD, HTML) guardados en Storage; se re-leen al re-indexar.
- website: rastreo del mismo dominio (respeta robots.txt, usa sitemap.xml si existe, máx. páginas), con el mismo
  fetch seguro del onboarding (sin redes internas, revalida redirecciones).
- catalog: un documento por producto (precio, stock, descripción); productos borrados salen del índice.
- conversations: conversaciones cerradas con tipificación de venta/éxito y buen puntaje de calidad → preguntas y
  respuestas anonimizadas (sin teléfonos, correos, documentos ni placas).
- legacy_docs: los documentos de la base de conocimiento anterior (knowledge_docs), sincronizados.
- faq: pares pregunta/respuesta en config.items (también los crea «Responder» un vacío).
- api: documentos enviados por la API.

Un documento cuyo texto no cambió (checksum) no se vuelve a fragmentar ni a pagar embeddings.
"""

import asyncio
import logging
import re
import urllib.robotparser
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import embeddings, parsers
from app.knowledge.text import checksum, chunk_text, clean, language, scrub_pii, tokens
from app.models import (
    AIAgentKnowledge,
    Conversation,
    KnowledgeChunk,
    KnowledgeDoc,
    KnowledgeDocument,
    KnowledgeSource,
    Message,
    Product,
    Typification,
    utcnow,
)

log = logging.getLogger(__name__)

SOURCE_TYPES = ("upload", "website", "catalog", "conversations", "legacy_docs", "faq", "api")
DEFAULT_MAX_PAGES = 30
MAX_PAGES_CAP = 200
CONVERSATIONS_PER_SYNC = 200
LOOP_EVERY_S = 300
_ASSET = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|zip|mp4|mp3|woff2?|ttf|pdf|xml)(\?|$)", re.I)


class IngestError(Exception):
    """Error de ingesta con mensaje para el usuario."""


# --- Índice ----------------------------------------------------------------------------------------------------
async def index_document(session: AsyncSession, doc: KnowledgeDocument, text: str, *, force: bool = False) -> bool:
    """Fragmenta y genera embeddings del texto del documento. False si no cambió (checksum) y no se forzó.
    Sin proveedor de embeddings los fragmentos se guardan igual (la búsqueda por texto funciona)."""
    text = clean(text)
    digest = checksum(text)
    if not force and doc.checksum == digest and doc.status == "ready":
        return False
    if not text:
        doc.status, doc.error, doc.chunks_count = "failed", "El documento no tiene texto", 0
        return True
    chunks = chunk_text(text, title=doc.title)
    doc.status, doc.error = "processing", None
    emb = await embeddings.get_embedder(session, doc.organization_id)
    vectors: list[list[float] | None] = [None] * len(chunks)
    model = None
    if emb is not None and chunks:
        try:
            res = await embeddings.embed(emb, doc.organization_id,
                                         [f"{c.heading}\n{c.content}" if c.heading else c.content for c in chunks],
                                         "document")
            vectors, model = res.vectors, res.model
        except embeddings.EmbeddingError as e:
            doc.status, doc.error = "failed", f"Embeddings: {e}"[:2000]
            return True
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    for c, v in zip(chunks, vectors, strict=True):
        session.add(KnowledgeChunk(organization_id=doc.organization_id, document_id=doc.id, ordinal=c.ordinal,
                                   heading=(c.heading or "")[:300] or None, content=c.content, tokens=c.tokens,
                                   embedding=v, embedding_model=model))
    doc.checksum, doc.language, doc.tokens = digest, language(text), tokens(text)
    doc.chunks_count, doc.status, doc.updated_at = len(chunks), "ready", utcnow()
    if emb is None:
        doc.metadata_ = {**(doc.metadata_ or {}), "embeddings": "sin proveedor: solo búsqueda por texto"}
    return True


async def upsert_document(session: AsyncSession, source: KnowledgeSource, *, uri: str, title: str, text: str,
                          mime: str | None = None, storage_path: str | None = None, metadata: dict | None = None,
                          valid_until: datetime | None = None, force: bool = False) -> tuple[KnowledgeDocument, bool]:
    """Crea o actualiza el documento (por fuente + uri) e indexa si su texto cambió. (documento, cambió)."""
    doc = await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source_id == source.id,
                                                              KnowledgeDocument.uri == uri))
    if doc is None:
        doc = KnowledgeDocument(organization_id=source.organization_id, source_id=source.id, uri=uri,
                                title=(title or uri)[:500], status="pending", metadata_={})
        session.add(doc)
        await session.flush()
    doc.title = (title or doc.title)[:500]
    if mime:
        doc.mime = mime
    if storage_path:
        doc.storage_path = storage_path
    if metadata:
        doc.metadata_ = {**(doc.metadata_ or {}), **metadata}
    if valid_until is not None:
        doc.valid_until = valid_until
    changed = await index_document(session, doc, text, force=force)
    return doc, changed


async def refresh_counts(session: AsyncSession, source: KnowledgeSource) -> None:
    row = (await session.execute(
        select(func.count(KnowledgeDocument.id), func.coalesce(func.sum(KnowledgeDocument.chunks_count), 0))
        .where(KnowledgeDocument.source_id == source.id, KnowledgeDocument.status != "excluded"))).one()
    source.documents_count, source.chunks_count = int(row[0]), int(row[1])
    # Las fuentes alimentadas por archivos / API / FAQ no se «sincronizan» como un sitio web: quedan listas en cuanto
    # tienen contenido indexado
    if source.type in ("upload", "api", "faq") and source.chunks_count and source.status in ("idle", "error"):
        source.status, source.last_synced_at, source.last_error = "ready", utcnow(), None


async def _drop_missing(session: AsyncSession, source: KnowledgeSource, keep: set[str]) -> int:
    """Documentos que ya no existen en el origen (página, producto, documento anterior) salen del índice."""
    stale = (await session.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.source_id == source.id, KnowledgeDocument.uri.not_in(keep or {"__none__"})))).all()
    for d in stale:
        await session.delete(d)
    return len(stale)


# --- Archivos ----------------------------------------------------------------------------------------------------
async def index_upload(session: AsyncSession, doc: KnowledgeDocument, force: bool = False) -> bool:
    """Re-lee el archivo de Storage y lo indexa (lo usa el trabajo después de subirlo)."""
    from app import storage

    if not doc.storage_path:
        raise IngestError("El documento no tiene archivo")
    data = await storage.download(doc.storage_path)
    try:
        text, mime = parsers.parse(doc.metadata_.get("filename") or doc.title, data)
    except parsers.ParseError as e:
        doc.status, doc.error = "failed", str(e)
        return True
    doc.mime = mime
    return await index_document(session, doc, text, force=force)


# --- Sitio web ---------------------------------------------------------------------------------------------------
async def _fetch(url: str) -> tuple[str, str]:
    """Fetch seguro del onboarding (solo http(s), sin redes internas, redirecciones revalidadas)."""
    from app.onboarding import website

    try:
        return await website.fetch(url)
    except website.ImportError_ as e:
        raise IngestError(str(e)) from e


def _links(base: str, page: str, host: str) -> list[str]:
    out = []
    for href in re.findall(r'(?is)<a[^>]+href=["\']([^"\'#]+)', page or ""):
        url = urljoin(base, href.strip())
        u = urlparse(url)
        if u.scheme in ("http", "https") and (u.hostname or "").lower() == host and not _ASSET.search(u.path):
            out.append(u._replace(fragment="").geturl())
    return out


async def crawl(url: str, max_pages: int = DEFAULT_MAX_PAGES, include: list[str] | None = None,
                exclude: list[str] | None = None) -> list[tuple[str, str, str]]:
    """[(url, título, texto)] del mismo dominio: robots.txt respetado, sitemap primero, luego enlaces (BFS)."""
    from app.onboarding.website import normalize_url

    start = normalize_url(url)
    host = (urlparse(start).hostname or "").lower()
    if not host:
        raise IngestError("URL inválida")
    origin = f"{urlparse(start).scheme}://{urlparse(start).netloc}"
    robots = urllib.robotparser.RobotFileParser()
    try:
        _, rtxt = await _fetch(origin + "/robots.txt")
        robots.parse(rtxt.splitlines())
    except IngestError:
        robots.parse([])

    def allowed(u: str) -> bool:
        path = urlparse(u).path or "/"
        if include and not any(p in path for p in include):
            return u == start
        if exclude and any(p in path for p in exclude):
            return False
        return robots.can_fetch("WA-Agent-Knowledge", u)

    queue: list[str] = [start]
    try:
        _, sitemap = await _fetch(origin + "/sitemap.xml")
        queue += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap)
                  if (urlparse(u).hostname or "").lower() == host]
    except IngestError:
        pass
    seen, pages = set(), []
    max_pages = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), MAX_PAGES_CAP))
    while queue and len(pages) < max_pages:
        u = queue.pop(0)
        key = u.rstrip("/")
        if key in seen or not allowed(u):
            continue
        seen.add(key)
        try:
            final, page = await _fetch(u)
        except IngestError as e:
            if u == start:
                raise
            log.info("Página omitida %s: %s", u, e)
            continue
        text = parsers.html_to_text(page)
        if len(text) >= 80:
            pages.append((final, parsers.html_title(page) or final, text))
        queue += [link for link in _links(final, page, host) if link.rstrip("/") not in seen]
    return pages


# --- Fuentes -----------------------------------------------------------------------------------------------------
async def _sync_website(session: AsyncSession, src: KnowledgeSource) -> dict:
    cfg = src.config or {}
    if not cfg.get("url"):
        raise IngestError("Indica la URL del sitio")
    pages = await crawl(cfg["url"], cfg.get("max_pages") or DEFAULT_MAX_PAGES, cfg.get("include"), cfg.get("exclude"))
    changed = 0
    for url, title, text in pages:
        _, did = await upsert_document(session, src, uri=url, title=title, text=text, mime="text/html")
        changed += did
    removed = await _drop_missing(session, src, {u for u, _, _ in pages})
    return {"documents": len(pages), "changed": changed, "removed": removed}


def _product_text(p: Product) -> str:
    lines = [f"# {p.name}", f"Referencia (SKU): {p.sku}"]
    if p.brand:
        lines.append(f"Marca: {p.brand}")
    if p.category:
        lines.append(f"Categoría: {p.category}")
    price = p.sale_price or p.price
    if price is not None:
        lines.append(f"Precio: {p.currency} {float(price):,.0f}".replace(",", ".") + (" (oferta)" if p.sale_price else ""))
    lines.append("Disponible: sí" if p.available else "Disponible: no")
    if p.stock is not None:
        lines.append(f"Unidades en inventario: {p.stock}")
    for k, v in list((getattr(p, "attributes", None) or {}).items())[:20]:
        lines.append(f"{k}: {v}")
    if p.description:
        lines.append(p.description)
    if p.url:
        lines.append(f"Más información: {p.url}")
    return "\n".join(lines)


async def _sync_catalog(session: AsyncSession, src: KnowledgeSource) -> dict:
    products = (await session.scalars(select(Product).where(Product.organization_id == src.organization_id))).all()
    changed = 0
    for p in products:
        _, did = await upsert_document(session, src, uri=f"sku:{p.sku}", title=p.name, text=_product_text(p),
                                       metadata={"product_id": p.id, "sku": p.sku})
        changed += did
    removed = await _drop_missing(session, src, {f"sku:{p.sku}" for p in products})
    return {"documents": len(products), "changed": changed, "removed": removed}


async def _sync_conversations(session: AsyncSession, src: KnowledgeSource) -> dict:
    from app.settings_store import get_setting

    cfg = src.config or {}
    kcfg = await get_setting(session, "knowledge", src.organization_id)
    min_score = float(cfg.get("min_qa_score", kcfg.get("conversations_min_qa_score", 80)))
    since = utcnow() - timedelta(days=int(cfg.get("days") or 90))
    q = (select(Conversation, Typification.name).join(Typification, Typification.id == Conversation.typification_id)
         .where(Conversation.organization_id == src.organization_id, Conversation.status == "closed",
                Conversation.closed_at >= since,
                (Conversation.qa_score >= min_score) | Conversation.qa_score.is_(None)))
    names = [n for n in (cfg.get("typifications") or []) if n]
    q = q.where(Typification.name.in_(names)) if names else q.where(Typification.is_success.is_(True))
    rows = (await session.execute(q.order_by(Conversation.closed_at.desc()).limit(CONVERSATIONS_PER_SYNC))).all()
    changed = 0
    for conv, typ in rows:
        msgs = (await session.scalars(select(Message).where(
            Message.conversation_id == conv.id, Message.sender_type != "system").order_by(Message.created_at))).all()
        lines = []
        for m in msgs:
            body = (m.text or m.transcript or "").strip()
            if body:
                who = "Cliente" if m.direction == "in" else "Empresa"
                lines.append(f"{who}: {scrub_pii(body)}")
        if len(lines) < 2:
            continue
        _, did = await upsert_document(session, src, uri=f"conv:{conv.id}",
                                       title=f"Conversación resuelta #{conv.id} — {typ}",
                                       text="\n\n".join(lines), metadata={"conversation_id": conv.id, "typification": typ})
        changed += did
    return {"documents": len(rows), "changed": changed, "removed": 0}


async def _sync_legacy(session: AsyncSession, src: KnowledgeSource) -> dict:
    docs = (await session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.organization_id == src.organization_id,
                                                             KnowledgeDoc.enabled))).all()
    links: dict[int, list[int]] = {}
    for doc_id, agent_id in (await session.execute(select(AIAgentKnowledge.doc_id, AIAgentKnowledge.ai_agent_id)
                                                   .where(AIAgentKnowledge.doc_id.in_([d.id for d in docs] or [0])))).all():
        links.setdefault(doc_id, []).append(agent_id)
    changed = 0
    for d in docs:
        _, did = await upsert_document(session, src, uri=f"legacy:{d.id}", title=d.title, text=d.content,
                                       metadata={"legacy_doc_id": d.id, "ai_agent_ids": sorted(links.get(d.id, []))})
        changed += did
    removed = await _drop_missing(session, src, {f"legacy:{d.id}" for d in docs})
    return {"documents": len(docs), "changed": changed, "removed": removed}


async def _sync_faq(session: AsyncSession, src: KnowledgeSource) -> dict:
    items = [i for i in (src.config or {}).get("items") or [] if (i.get("question") or "").strip()]
    changed, keep = 0, set()
    for item in items:
        uri = "faq:" + checksum(item["question"].strip().lower())[:16]
        keep.add(uri)
        text = f"Pregunta: {item['question'].strip()}\n\nRespuesta: {(item.get('answer') or '').strip()}"
        _, did = await upsert_document(session, src, uri=uri, title=item["question"].strip()[:200], text=text)
        changed += did
    removed = await _drop_missing(session, src, keep)
    return {"documents": len(items), "changed": changed, "removed": removed}


async def _sync_uploads(session: AsyncSession, src: KnowledgeSource, force: bool) -> dict:
    docs = (await session.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.source_id == src.id, KnowledgeDocument.storage_path.is_not(None),
        KnowledgeDocument.status != "excluded"))).all()
    changed = 0
    for d in docs:
        if force or d.status in ("pending", "failed"):
            changed += await index_upload(session, d, force=force)
    return {"documents": len(docs), "changed": changed, "removed": 0}


async def sync_source(source_id: int, force: bool = False) -> dict:
    """Sincroniza una fuente completa (lo ejecuta el trabajo knowledge.sync_source)."""
    from app.db import SessionLocal

    async with SessionLocal() as session:
        src = await session.get(KnowledgeSource, source_id)
        if not src or not src.is_active:
            return {"skipped": True}
        src.status, src.last_error = "syncing", None
        await session.commit()
        try:
            if src.type == "website":
                result = await _sync_website(session, src)
            elif src.type == "catalog":
                result = await _sync_catalog(session, src)
            elif src.type == "conversations":
                result = await _sync_conversations(session, src)
            elif src.type == "legacy_docs":
                result = await _sync_legacy(session, src)
            elif src.type == "faq":
                result = await _sync_faq(session, src)
            elif src.type == "upload":
                result = await _sync_uploads(session, src, force)
            else:  # api: los documentos llegan por la API
                result = {"documents": src.documents_count, "changed": 0, "removed": 0}
            src.status, src.last_synced_at = "ready", utcnow()
        except (IngestError, embeddings.EmbeddingError) as e:
            await session.rollback()
            src = await session.get(KnowledgeSource, source_id)
            src.status, src.last_error = "error", str(e)[:2000]
            result = {"error": str(e)}
        await refresh_counts(session, src)
        await session.commit()
        return result


async def schedule_sync(session: AsyncSession, src: KnowledgeSource, force: bool = False) -> None:
    """Encola la sincronización (cola «ai»); sin cola de trabajos corre en segundo plano en este proceso."""
    from app import jobs

    if jobs.enabled():
        await jobs.enqueue(session, "knowledge.sync_source", {"source_id": src.id, "force": force},
                           organization_id=src.organization_id, dedupe_key=f"ks:{src.id}")
    else:
        _spawn(sync_source(src.id, force))


async def schedule_index(session: AsyncSession, doc: KnowledgeDocument) -> None:
    from app import jobs

    if jobs.enabled():
        await jobs.enqueue(session, "knowledge.index_document", {"document_id": doc.id},
                           organization_id=doc.organization_id, dedupe_key=f"kd:{doc.id}")
    else:
        _spawn(index_document_job(doc.id))


async def index_document_job(document_id: int, force: bool = False) -> dict:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        doc = await session.get(KnowledgeDocument, document_id)
        if not doc:
            return {"skipped": True}
        try:
            changed = await index_upload(session, doc, force=force)
        except (IngestError, embeddings.EmbeddingError) as e:
            doc.status, doc.error = "failed", str(e)[:2000]
            changed = True
        src = await session.get(KnowledgeSource, doc.source_id)
        await refresh_counts(session, src)
        await session.commit()
        return {"status": doc.status, "changed": changed}


_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# --- Programador -------------------------------------------------------------------------------------------------
async def ensure_legacy_source(session: AsyncSession, org: int) -> KnowledgeSource | None:
    """La base de conocimiento anterior sigue visible como fuente «Documentos anteriores»."""
    src = await session.scalar(select(KnowledgeSource).where(KnowledgeSource.organization_id == org,
                                                             KnowledgeSource.type == "legacy_docs"))
    if src:
        return src
    has_docs = await session.scalar(select(func.count()).select_from(KnowledgeDoc).where(
        KnowledgeDoc.organization_id == org))
    if not has_docs:
        return None
    src = KnowledgeSource(organization_id=org, type="legacy_docs", name="Documentos anteriores", config={},
                          refresh_hours=None)
    session.add(src)
    await session.flush()
    return src


async def due_sources() -> list[tuple[int, int]]:
    """(source_id, org) a sincronizar: con refresh_hours vencido, o documentos anteriores editados después de la
    última sincronización (se mantienen al día solos)."""
    from app.db import SessionLocal

    async with SessionLocal() as session:
        orgs = (await session.scalars(select(KnowledgeDoc.organization_id).distinct())).all()
        for org in orgs:
            await ensure_legacy_source(session, org)
        await session.commit()
        now = utcnow()
        out = []
        for src in (await session.scalars(select(KnowledgeSource).where(
                KnowledgeSource.is_active, KnowledgeSource.status != "syncing"))).all():
            if src.type == "legacy_docs":
                last = await session.scalar(select(func.max(KnowledgeDoc.updated_at)).where(
                    KnowledgeDoc.organization_id == src.organization_id))
                count_docs = await session.scalar(select(func.count()).select_from(KnowledgeDoc).where(
                    KnowledgeDoc.organization_id == src.organization_id, KnowledgeDoc.enabled))
                if src.last_synced_at is None or (last and last > src.last_synced_at) or \
                        count_docs != src.documents_count:
                    out.append((src.id, src.organization_id))
            elif src.refresh_hours and (src.last_synced_at is None or
                                        src.last_synced_at < now - timedelta(hours=src.refresh_hours)):
                out.append((src.id, src.organization_id))
        return out


async def knowledge_loop() -> None:
    from app import jobs

    while True:
        try:
            due = await due_sources()
            if jobs.enabled():
                await jobs.schedule("knowledge.sync_source", [({"source_id": s, "force": False}, f"ks:{s}", o)
                                                             for s, o in due])
            else:
                for s, _ in due:
                    await sync_source(s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Bucle de la base de conocimiento")
        await asyncio.sleep(LOOP_EVERY_S)
