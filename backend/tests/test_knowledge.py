"""Base de conocimiento con RAG: fragmentación, lectores de archivos, rastreo seguro, fuentes, búsqueda híbrida,
citas del agente de IA, vacíos y aislamiento. Embeddings simulados (FakeEmbedder) y empresas propias."""

import io
import zipfile
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import jobs
from app.ai import router as ai_router
from app.ai.base import AgentResult
from app.auth import hash_password
from app.db import SessionLocal
from app.knowledge import embeddings, gaps, ingest, parsers, retrieve
from app.knowledge.text import chunk_text, scrub_pii, tokens
from app.main import app
from app.models import (
    Agent,
    AIAgent,
    AICall,
    Channel,
    Contact,
    Conversation,
    Cortex,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeGap,
    KnowledgeQuery,
    KnowledgeSource,
    Message,
    Organization,
    Product,
    Typification,
    utcnow,
)
from tests.conftest import WA, unique_phone

ORG, OTHER = 9610, 9611
PWD = "Conocimiento-9610!"
IDS: dict = {}

HOURS = ("# Horarios\n\nLa sala de ventas abre de lunes a viernes de 8:00 a 18:00 y los sábados de 9:00 a 13:00. "
         "Los domingos y festivos el concesionario permanece cerrado.")
WARRANTY = ("# Garantía\n\nTodos los vehículos nuevos tienen garantía de fábrica de 5 años o 100.000 kilómetros. "
            "La garantía cubre motor, caja y sistema eléctrico; no cubre desgaste de frenos ni llantas.")


@pytest.fixture(autouse=True)
def fake_embeddings():
    fake = embeddings.FakeEmbedder()
    embeddings.set_override(fake)
    yield fake
    embeddings.set_override(None)


async def _seed() -> dict:
    if IDS:
        return IDS
    async with SessionLocal() as s:
        s.add_all([Organization(id=ORG, name="Concesionario RAG", slug="org-rag", onboarding_completed_at=utcnow()),
                   Organization(id=OTHER, name="Otra RAG", slug="otra-rag", onboarding_completed_at=utcnow())])
        await s.flush()
        admin = Agent(organization_id=ORG, email="admin@rag9610.co", name="Admin RAG", role="admin",
                      password_hash=hash_password(PWD))
        other_admin = Agent(organization_id=OTHER, email="admin@rag9611.co", name="Admin Otra", role="admin",
                            password_hash=hash_password(PWD))
        ch = Channel(organization_id=ORG, name="WA 9610", phone_number_id="PN9610")
        s.add_all([admin, other_admin, ch])
        await s.flush()
        s.add(Cortex(organization_id=ORG, name="Chat RAG", purpose="chat"))
        bot_a = AIAgent(organization_id=ORG, name="Ventas", system_prompt="Eres el asistente de ventas.",
                        cost_optimization=False, security_enabled=False, use_catalog=False, use_appointments=False)
        bot_b = AIAgent(organization_id=ORG, name="Taller", system_prompt="Eres el asistente del taller.",
                        cost_optimization=False, security_enabled=False)
        s.add_all([bot_a, bot_b])
        await s.flush()
        await s.commit()
        IDS.update(admin=admin.id, channel=ch.id, bot_a=bot_a.id, bot_b=bot_b.id)
    return IDS


async def _login(email: str = "admin@rag9610.co") -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": email, "password": PWD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


async def _source(org: int, type_: str, name: str, **kw) -> int:
    async with SessionLocal() as s:
        src = KnowledgeSource(organization_id=org, type=type_, name=name, config=kw.pop("config", {}), **kw)
        s.add(src)
        await s.commit()
        return src.id


async def _put(source_id: int, uri: str, title: str, text: str, **kw) -> int:
    async with SessionLocal() as s:
        src = await s.get(KnowledgeSource, source_id)
        doc, _ = await ingest.upsert_document(s, src, uri=uri, title=title, text=text, **kw)
        await ingest.refresh_counts(s, src)
        await s.commit()
        return doc.id


# --- Texto y archivos ------------------------------------------------------------------------------------------
def test_chunking_sections_size_and_overlap():
    paras = [" ".join(f"El vehículo {i}-{j} cuenta con frenos ABS, seis bolsas de aire y control de estabilidad."
                      for j in range(12)) for i in range(6)]
    text = "# Seguridad\n\n" + "\n\n".join(paras) + "\n\n# Financiación\n\nCuota inicial desde el 10 por ciento."
    chunks = chunk_text(text, title="Ficha")
    assert len(chunks) >= 3
    assert all(c.tokens <= 800 for c in chunks)
    assert chunks[0].heading == "Seguridad" and chunks[-1].heading == "Financiación"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # solapamiento: el final de un fragmento reaparece al inicio del siguiente de la misma sección
    a, b = chunks[0].content, chunks[1].content
    assert a[-60:].split()[-3] in b[:400]
    assert tokens("abcd" * 100) == 100


def test_scrub_pii_keeps_prices():
    out = scrub_pii("Soy Ana, mi cel 3001234567, correo ana.ruiz@mail.com, placa ABC123, CC 1020304050. "
                    "El carro vale $ 85.900.000")
    assert "3001234567" not in out and "ana.ruiz@mail.com" not in out and "ABC123" not in out
    assert "1020304050" not in out and "85.900.000" in out


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def _docx(paragraphs: list[tuple[str | None, str]]) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p>{f"<w:pPr><w:pStyle w:val=\"{style}\"/></w:pPr>" if style else ""}<w:r><w:t>{t}</w:t></w:r></w:p>'
        for style, t in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Precios"
    ws.append(["Modelo", "Precio", "Color"])
    ws.append(["Onix LTZ", 85900000, "Rojo"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parsers_each_format():
    text, mime = parsers.parse("manual.pdf", _pdf("Garantia de cinco anos"))
    assert "Garantia de cinco anos" in text and mime == "application/pdf"
    text, _ = parsers.parse("politicas.docx", _docx([("Heading1", "Devoluciones"), (None, "Tienes 30 días.")]))
    assert "# Devoluciones" in text and "Tienes 30 días." in text
    text, _ = parsers.parse("lista.xlsx", _xlsx())
    assert "# Precios" in text and "Modelo: Onix LTZ; Precio: 85900000; Color: Rojo" in text
    text, _ = parsers.parse("lista.csv", "Modelo;Precio\nTracker;120000000\n".encode())
    assert "Modelo: Tracker; Precio: 120000000" in text
    text, _ = parsers.parse("pagina.html", b"<html><script>x()</script><h2>Servicio</h2><p>Lunes a viernes</p></html>")
    assert "## Servicio" in text and "x()" not in text
    assert parsers.parse("nota.md", "# Hola\n\nTexto".encode())[0].startswith("# Hola")
    with pytest.raises(parsers.ParseError):
        parsers.parse("virus.exe", b"MZ")
    with pytest.raises(parsers.ParseError):
        parsers.parse("vacio.txt", b"   ")


# --- Índice y fuentes ------------------------------------------------------------------------------------------
async def test_checksum_skip_and_cost_logged(fake_embeddings):
    await _seed()
    sid = await _source(ORG, "api", "API checksum")
    before = len(fake_embeddings.calls)
    async with SessionLocal() as s:
        calls0 = await s.scalar(select(func.count()).select_from(AICall).where(
            AICall.organization_id == ORG, AICall.purpose == "embedding"))
    doc_id = await _put(sid, "api:1", "Políticas", WARRANTY)
    assert len(fake_embeddings.calls) == before + 1
    await _put(sid, "api:1", "Políticas", WARRANTY)  # mismo texto: no se re-fragmenta ni se paga
    assert len(fake_embeddings.calls) == before + 1
    await _put(sid, "api:1", "Políticas", WARRANTY + "\n\nIncluye asistencia en carretera.")
    assert len(fake_embeddings.calls) == before + 2
    async with SessionLocal() as s:
        doc = await s.get(KnowledgeDocument, doc_id)
        assert doc.status == "ready" and doc.chunks_count >= 1 and doc.language == "es"
        chunk = await s.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc_id))
        assert chunk.embedding is not None and chunk.embedding_model == "fake-embed"
        calls = (await s.scalars(select(AICall).where(AICall.organization_id == ORG, AICall.purpose == "embedding")
                                 .order_by(AICall.id.desc()))).all()
        assert len(calls) == calls0 + 2 and calls[0].status == "ok" and float(calls[0].cost_usd) > 0
        assert calls[0].input_tokens > 0


async def test_crawl_robots_sitemap_same_domain(monkeypatch):
    pages = {
        "https://autos.example/robots.txt": "User-agent: *\nDisallow: /privado",
        "https://autos.example/sitemap.xml": "<urlset><url><loc>https://autos.example/servicios</loc></url>"
                                             "<url><loc>https://otro.example/x</loc></url></urlset>",
        "https://autos.example": "<title>Inicio</title><h1>Autos</h1><p>" + "Concesionario oficial. " * 10 +
                                 '</p><a href="/privado/admin">x</a><a href="https://otro.example/y">y</a>'
                                 '<a href="/logo.png">l</a><a href="/financiacion">f</a>',
        "https://autos.example/servicios": "<title>Servicios</title><p>" + "Taller y repuestos. " * 10 + "</p>",
        "https://autos.example/financiacion": "<title>Financiación</title><p>" + "Crédito al 1%. " * 10 + "</p>",
    }
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        key = url.rstrip("/")
        if key not in pages:
            raise ingest.IngestError("404")
        return url, pages[key]

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    out = await ingest.crawl("https://autos.example/", max_pages=10)
    urls = {u.rstrip("/") for u, _, _ in out}
    assert urls == {"https://autos.example", "https://autos.example/servicios", "https://autos.example/financiacion"}
    assert not any("privado" in u or "otro.example" in u or "logo.png" in u for u in fetched)
    assert len(await ingest.crawl("https://autos.example/", max_pages=1)) == 1


async def test_ssrf_blocked():
    for url in ("http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"):
        with pytest.raises(ingest.IngestError):
            await ingest._fetch(url)
    await _seed()
    sid = await _source(ORG, "website", "Interno", config={"url": "http://10.0.0.5/"})
    await ingest.sync_source(sid)
    async with SessionLocal() as s:
        src = await s.get(KnowledgeSource, sid)
        assert src.status == "error" and src.last_error


async def test_catalog_and_conversations_with_pii_scrubbed():
    await _seed()
    async with SessionLocal() as s:
        s.add_all([Product(organization_id=ORG, sku="ONIX-RAG", name="Onix RAG", price=85900000, stock=3,
                           description="Sedán compacto turbo"),
                   Product(organization_id=ORG, sku="TRK-RAG", name="Tracker RAG", price=120000000)])
        ok = Typification(organization_id=ORG, name="Venta RAG", is_success=True)
        bad = Typification(organization_id=ORG, name="No interesado RAG", is_success=False)
        s.add_all([ok, bad])
        c = Contact(organization_id=ORG, wa_id="573019610001", name="Ana")
        s.add(c)
        await s.flush()
        good = Conversation(organization_id=ORG, contact_id=c.id, channel_id=IDS["channel"], status="closed",
                            typification_id=ok.id, closed_at=utcnow(), qa_score=92)
        low = Conversation(organization_id=ORG, contact_id=c.id, channel_id=IDS["channel"], status="closed",
                           typification_id=ok.id, closed_at=utcnow(), qa_score=40)
        lost = Conversation(organization_id=ORG, contact_id=c.id, channel_id=IDS["channel"], status="closed",
                            typification_id=bad.id, closed_at=utcnow(), qa_score=95)
        s.add_all([good, low, lost])
        await s.flush()
        for conv in (good, low, lost):
            s.add_all([Message(organization_id=ORG, conversation_id=conv.id, direction="in", sender_type="contact",
                               text="¿Reciben carro usado en parte de pago? mi cel es 3001234567 y placa XYZ987"),
                       Message(organization_id=ORG, conversation_id=conv.id, direction="out", sender_type="agent",
                               text="Sí, recibimos tu usado; escríbenos a ventas@rag.co o al 3109876543")])
        await s.commit()
        good_id, low_id, lost_id = good.id, low.id, lost.id
    cat = await _source(ORG, "catalog", "Catálogo")
    conv_src = await _source(ORG, "conversations", "Conversaciones")
    assert (await ingest.sync_source(cat))["documents"] >= 2
    await ingest.sync_source(conv_src)
    async with SessionLocal() as s:
        uris = set((await s.scalars(select(KnowledgeDocument.uri).where(KnowledgeDocument.source_id == cat))).all())
        assert {"sku:ONIX-RAG", "sku:TRK-RAG"} <= uris
        docs = (await s.scalars(select(KnowledgeDocument).where(KnowledgeDocument.source_id == conv_src))).all()
        assert {d.uri for d in docs} == {f"conv:{good_id}"}
        assert f"conv:{low_id}" not in {d.uri for d in docs} and f"conv:{lost_id}" not in {d.uri for d in docs}
        text = " ".join((await s.scalars(select(KnowledgeChunk.content).where(
            KnowledgeChunk.document_id == docs[0].id))).all())
        for secret in ("3001234567", "XYZ987", "ventas@rag.co", "3109876543"):
            assert secret not in text
        assert "parte de pago" in text
        # producto borrado sale del índice en la siguiente sincronización
        await s.execute(Product.__table__.delete().where(Product.sku == "TRK-RAG", Product.organization_id == ORG))
        await s.commit()
    await ingest.sync_source(cat)
    async with SessionLocal() as s:
        uris = set((await s.scalars(select(KnowledgeDocument.uri).where(KnowledgeDocument.source_id == cat))).all())
        assert "sku:TRK-RAG" not in uris and "sku:ONIX-RAG" in uris


# --- Búsqueda --------------------------------------------------------------------------------------------------
async def test_hybrid_ranking_validity_and_agent_filter():
    ids = await _seed()
    sid = await _source(ORG, "api", "Políticas búsqueda")
    hours = await _put(sid, "api:hours", "Horarios de atención", HOURS)
    warranty = await _put(sid, "api:warranty", "Garantía", WARRANTY)
    promo = await _put(sid, "api:promo", "Promoción de mayo",
                       "# Promoción\n\nBono de descuento de 5 millones en la compra de cualquier camioneta.",
                       valid_until=utcnow() - timedelta(days=1))
    taller = await _source(ORG, "api", "Solo taller", ai_agent_ids=[ids["bot_b"]])
    await _put(taller, "api:taller", "Garantía del taller", "# Taller\n\nLa garantía del taller cubre 6 meses "
               "de mano de obra en reparaciones de garantía.")
    async with SessionLocal() as s:
        r = await retrieve.search(s, ORG, "¿Cuántos años de garantía tienen los carros nuevos?", log_query=False)
        assert r.passages and r.passages[0].label == "S1" and "5 años" in r.passages[0].content
        assert warranty in {p.document_id for p in r.passages}
        assert r.embedding is not None and r.top_score > 0
        r = await retrieve.search(s, ORG, "¿a qué hora abren el sábado?", log_query=False)
        assert "sábados de 9:00" in r.passages[0].content and hours in {p.document_id for p in r.passages}
        r = await retrieve.search(s, ORG, "bono de descuento camioneta", log_query=False)
        assert promo not in {p.document_id for p in r.passages}  # vencido: no se usa
        r = await retrieve.search(s, ORG, "garantía del taller mano de obra", ai_agent_id=ids["bot_a"],
                                  log_query=False)
        assert all(p.source_id != taller for p in r.passages)
        r = await retrieve.search(s, ORG, "garantía del taller mano de obra", ai_agent_id=ids["bot_b"],
                                  log_query=False)
        assert any(p.source_id == taller for p in r.passages)
        assert "[S1]" in r.prompt_block() and "[[fuentes:" in r.prompt_block()


def test_strip_citations():
    clean, used = retrieve.strip_citations("Abrimos a las 8 [S1].\n\n[[fuentes: S1, S3]]")
    assert clean == "Abrimos a las 8." and used == ["S1", "S3"]
    clean, used = retrieve.strip_citations("No tengo esa información.\n[[fuentes: ninguna]]")
    assert clean == "No tengo esa información." and used == []
    assert retrieve.strip_citations("Hola")[1] is None


# --- Agente de IA ----------------------------------------------------------------------------------------------
async def _bot_conversation(text: str) -> int:
    ids = await _seed()
    async with SessionLocal() as s:
        c = Contact(organization_id=ORG, wa_id=unique_phone("5730"), name="Luis")
        s.add(c)
        await s.flush()
        conv = Conversation(organization_id=ORG, contact_id=c.id, channel_id=ids["channel"], status="bot",
                            ai_agent_id=ids["bot_a"])
        s.add(conv)
        await s.flush()
        s.add(Message(organization_id=ORG, conversation_id=conv.id, direction="in", sender_type="contact", text=text))
        await s.commit()
        return conv.id


async def test_agent_context_citations_and_fallback(monkeypatch):
    from app.agent import run_agent

    await _seed()
    sid = await _source(ORG, "api", "Políticas agente")
    await _put(sid, "api:hours-agent", "Horarios sala", HOURS)
    seen: list = []

    async def fake_chat(session, cx, req, execute_tool, ctx):
        seen.append(req.context)
        asked = " ".join(getattr(p, "text", "") for p in req.turns[-1].parts)
        if "sábado" in asked and "[S1]" in req.context:
            return AgentResult(text="Los sábados abrimos de 9:00 a 13:00 [S1].\n[[fuentes: S1]]")
        return AgentResult(text="No tengo esa información, ¿te paso con un asesor?\n[[fuentes: ninguna]]")

    monkeypatch.setattr(ai_router, "run_chat", fake_chat)
    WA.sent.clear()
    conv_id = await _bot_conversation("¿Qué horario tienen el sábado?")
    await run_agent(conv_id)
    assert "Base de conocimiento" in seen[-1] and "Horarios sala" in seen[-1]
    async with SessionLocal() as s:
        out = (await s.scalars(select(Message.text).where(Message.conversation_id == conv_id,
                                                          Message.direction == "out"))).all()
        assert out == ["Los sábados abrimos de 9:00 a 13:00."]  # sin la etiqueta de fuentes
        q = await s.scalar(select(KnowledgeQuery).where(KnowledgeQuery.conversation_id == conv_id))
        assert q.answered is True and q.ai_agent_id == IDS["bot_a"] and q.latency_ms is not None
        assert any(r.get("used") for r in q.results)

    conv2 = await _bot_conversation("¿Tienen convenio con la aseguradora Sura para pólizas?")
    await run_agent(conv2)
    async with SessionLocal() as s:
        out = (await s.scalars(select(Message.text).where(Message.conversation_id == conv2,
                                                          Message.direction == "out"))).all()
        assert out == ["No tengo esa información, ¿te paso con un asesor?"]
        q = await s.scalar(select(KnowledgeQuery).where(KnowledgeQuery.conversation_id == conv2))
        assert q.answered is False
        gap = await s.scalar(select(KnowledgeGap).where(KnowledgeGap.organization_id == ORG,
                                                        KnowledgeGap.topic.ilike("%aseguradora%")))
        assert gap is not None and gap.status == "open"

    conv3 = await _bot_conversation("hola")  # saludo: no se busca
    await run_agent(conv3)
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).select_from(KnowledgeQuery).where(
            KnowledgeQuery.conversation_id == conv3)) == 0


# --- Vacíos ----------------------------------------------------------------------------------------------------
async def test_gap_clustering_and_answer_creates_faq():
    await _seed()
    async with SessionLocal() as s:
        g1 = await gaps.record(s, ORG, "¿Hacen envíos a domicilio?",
                               embeddings.FakeEmbedder.vector("¿Hacen envíos a domicilio?"))
        g2 = await gaps.record(s, ORG, "hacen envios a domicilio",
                               embeddings.FakeEmbedder.vector("hacen envios a domicilio"))
        g3 = await gaps.record(s, ORG, "¿Cuál es el precio del seguro todo riesgo?",
                               embeddings.FakeEmbedder.vector("¿Cuál es el precio del seguro todo riesgo?"))
        await s.commit()
        assert g1.id == g2.id and g1.occurrences == 2 and len(g1.examples) == 2
        assert g3.id != g1.id
        gid = g1.id
    c = await _login()
    r = await c.get("/api/knowledge-base/gaps")
    assert r.status_code == 200 and any(g["id"] == gid and g["occurrences"] == 2 for g in r.json())
    r = await c.post(f"/api/knowledge-base/gaps/{gid}/answer",
                     json={"answer": "Sí, hacemos envíos a domicilio en Bogotá sin costo."})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        g = await s.get(KnowledgeGap, gid)
        assert g.status == "answered" and g.resolved_document_id
        res = await retrieve.search(s, ORG, "¿hacen envíos a domicilio?", log_query=False)
        assert res.passages and res.passages[0].document_id == g.resolved_document_id
        src = await s.get(KnowledgeSource, res.passages[0].source_id)
        assert src.type == "faq" and src.config["items"][0]["answer"].startswith("Sí, hacemos")
    r = await c.post(f"/api/knowledge-base/gaps/{g3.id}/ignore")
    assert r.status_code == 200
    assert all(g["id"] != g3.id for g in (await c.get("/api/knowledge-base/gaps")).json())
    await c.aclose()


# --- API y aislamiento -----------------------------------------------------------------------------------------
async def test_api_upload_sync_search_stats_and_isolation():
    await _seed()
    c = await _login()
    r = await c.post("/api/knowledge-base/sources", json={"type": "upload", "name": "Manuales"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await c.post(f"/api/knowledge-base/sources/{sid}/upload",
                     files=[("files", ("devoluciones.docx", _docx([("Heading1", "Devoluciones"),
                                                                   (None, "Aceptamos devoluciones de accesorios "
                                                                          "durante 30 días con factura.")]),
                                       "application/octet-stream"))])
    assert r.status_code == 200, r.text
    did = r.json()[0]["id"]
    assert r.json()[0]["status"] == "pending"
    await jobs.run_pending("ai")
    r = await c.get(f"/api/knowledge-base/documents/{did}")
    assert r.json()["status"] == "ready", r.json()
    chunks = (await c.get(f"/api/knowledge-base/documents/{did}/chunks")).json()
    assert chunks and chunks[0]["embedded"] and "30 días" in chunks[0]["content"]
    r = await c.post("/api/knowledge-base/search", json={"query": "¿puedo devolver un accesorio?"})
    assert r.status_code == 200 and r.json()["passages"][0]["document_id"] == did and r.json()["semantic"]
    r = await c.post(f"/api/knowledge-base/sources/{sid}/upload",
                     files=[("files", ("virus.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 422
    r = await c.patch(f"/api/knowledge-base/documents/{did}", json={"excluded": True})
    assert r.json()["status"] == "excluded"
    r = await c.post("/api/knowledge-base/search", json={"query": "¿puedo devolver un accesorio?"})
    assert all(p["document_id"] != did for p in r.json()["passages"])
    stats = (await c.get("/api/knowledge-base/stats")).json()
    assert stats["failed_documents"] == 0 and stats["embedding_tokens"] > 0 and "open_gaps" in stats
    r = await c.post("/api/knowledge-base/sources", json={"type": "website", "name": "Web", "config": {}})
    assert r.status_code == 422

    other = await _login("admin@rag9611.co")
    assert (await other.get(f"/api/knowledge-base/documents/{did}")).status_code == 404
    assert (await other.get(f"/api/knowledge-base/sources/{sid}")).status_code == 404
    assert (await other.post(f"/api/knowledge-base/sources/{sid}/sync")).status_code == 404
    assert all(s["id"] != sid for s in (await other.get("/api/knowledge-base/sources")).json())
    r = await other.post("/api/knowledge-base/search", json={"query": "garantía horarios devoluciones"})
    assert r.json()["passages"] == []
    r = await other.post("/api/knowledge-base/search", json={"query": "garantía", "source_ids": [sid]})
    assert r.status_code == 404
    await c.aclose()
    await other.aclose()


async def test_legacy_docs_visible_as_source():
    from app.models import AIAgentKnowledge, KnowledgeDoc

    ids = await _seed()
    async with SessionLocal() as s:
        d = KnowledgeDoc(organization_id=ORG, title="Políticas anteriores",
                         content="Los test drive se agendan con 24 horas de anticipación.")
        s.add(d)
        await s.flush()
        s.add(AIAgentKnowledge(ai_agent_id=ids["bot_b"], doc_id=d.id))
        await s.commit()
    c = await _login()
    sources = (await c.get("/api/knowledge-base/sources")).json()
    legacy = next(s for s in sources if s["type"] == "legacy_docs")
    await jobs.run_pending("ai")
    docs = (await c.get("/api/knowledge-base/documents", params={"source_id": legacy["id"]})).json()
    assert any(x["title"] == "Políticas anteriores" and x["status"] == "ready" for x in docs["items"])
    async with SessionLocal() as s:
        q = "¿con cuánta anticipación agendo el test drive?"
        assert any(p.title == "Políticas anteriores" for p in
                   (await retrieve.search(s, ORG, q, ai_agent_id=ids["bot_b"], log_query=False)).passages)
        assert all(p.title != "Políticas anteriores" for p in
                   (await retrieve.search(s, ORG, q, ai_agent_id=ids["bot_a"], log_query=False)).passages)
    assert (await c.delete(f"/api/knowledge-base/sources/{legacy['id']}")).status_code == 409
    await c.aclose()



async def test_regressions_search_path_and_natural_question():
    """Bugs encontrados en la prueba en navegador: (1) el tipo vector vive en `extensions` y cada conexión de la app
    debe verlo aunque el rol/pooler no lo incluya; (2) sin embeddings, una pregunta natural debe encontrar el
    fragmento aunque no repita todas sus palabras."""
    from sqlalchemy import text as sql

    from app.db import engine
    from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource

    async with engine.connect() as conn:
        assert "extensions" in (await conn.scalar(sql("show search_path")))

    async with SessionLocal() as s:
        src = KnowledgeSource(organization_id=1, type="upload", name="Regresión búsqueda")
        s.add(src)
        await s.flush()
        doc = KnowledgeDocument(organization_id=1, source_id=src.id, title="Garantía regresión", uri="reg:garantia",
                                status="ready")
        s.add(doc)
        await s.flush()
        s.add(KnowledgeChunk(organization_id=1, document_id=doc.id, ordinal=0, heading="Garantía",
                             content="La garantía del vehículo nuevo es de 3 años o 100.000 km.", embedding=None))
        await s.commit()
        rows = (await s.execute(sql("select document_id from public.knowledge_search(1, null, :q, 5, :src)"),
                                {"q": "¿cuánto dura la garantía?", "src": [src.id]})).all()
    assert [r[0] for r in rows] == [doc.id]
