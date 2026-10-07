"""Memoria del negocio, el mejor vendedor y catálogo de productos (importación, búsqueda, sincronización)."""

import asyncio
import io
import json
import re
from datetime import timedelta

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app import catalog, learning
from app.auth import hash_password
from app.db import SessionLocal
from app.models import Agent, Channel, Contact, Conversation, Message, Typification, utcnow

PLAYBOOK = {
    "persona": "Asesor cercano", "tone": "Cálido y directo",
    "sales_process": [{"stage": "Descubrir", "goal": "Entender la necesidad", "tactics": ["Preguntar uso"]}],
    "discovery_questions": ["¿Para qué usarás el vehículo?"],
    "objections": [{"objection": "Está caro", "response": "Mostrar financiación"}],
    "closing_techniques": ["Agendar test drive"], "do": ["Mensajes cortos"], "dont": ["Inventar precios"],
    "example_phrases": ["¿Te queda bien el sábado?"],
    "system_prompt": "Eres el mejor vendedor de la empresa. Descubre la necesidad y agenda un test drive.",
}
llm_inputs: list[str] = []


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    async def complete_json(session, cx, system, user, schema, ctx, max_tokens=4000):
        llm_inputs.append(user)
        assert ctx.purpose == "learning"
        if "items" in schema["properties"]:
            ids = [int(x) for x in re.findall(r"Conversación (\d+)", user)]
            return {"items": [
                {"kind": "objection", "title": "Precio alto", "content": "Ofrecer financiación a 60 meses.",
                 "conversation_ids": ids[:1] + [999999], "confidence": 0.8},
                {"kind": "faq", "title": "Horario del taller", "content": "Lunes a sábado 8-18.",
                 "conversation_ids": ids, "confidence": 0.9},
                {"kind": "inventado", "title": "x", "content": "y", "conversation_ids": [], "confidence": 1},
            ]}
        return dict(PLAYBOOK)

    monkeypatch.setattr(learning, "complete_json", complete_json)


async def wait_run(c, url: str) -> dict:
    for _ in range(50):
        run = (await c.get(url)).json()
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.05)
    raise AssertionError("la ejecución no terminó")


async def _seed_sales() -> tuple[int, int]:
    """Dos asesores: Luis con 3 ventas de 4 cierres, Marta con 1 de 3."""
    async with SessionLocal() as s:
        channel = await s.scalar(select(Channel).where(Channel.organization_id == 1).limit(1))
        venta = await s.scalar(select(Typification).where(Typification.organization_id == 1,
                                                          Typification.name == "Venta"))
        reclamo = await s.scalar(select(Typification).where(Typification.organization_id == 1,
                                                            Typification.name == "Reclamo"))
        luis = Agent(organization_id=1, email="luis.l@test.com", name="Luis L", password_hash=hash_password("x" * 8))
        marta = Agent(organization_id=1, email="marta.l@test.com", name="Marta L", password_hash=hash_password("x" * 8))
        s.add_all([luis, marta])
        await s.flush()
        n = 0
        for agent, outcomes in ((luis, [venta, venta, venta, reclamo]), (marta, [venta, reclamo, reclamo])):
            for typ in outcomes:
                n += 1
                contact = Contact(organization_id=1, wa_id=f"5714400{agent.id:03d}{n:03d}", name=f"Cliente {n}")
                s.add(contact)
                await s.flush()
                conv = Conversation(organization_id=1, contact_id=contact.id, channel_id=channel.id, status="human",
                                    assigned_agent_id=agent.id)
                s.add(conv)
                await s.flush()
                t0 = utcnow() - timedelta(hours=2)
                s.add_all([
                    Message(organization_id=1, conversation_id=conv.id, direction="in", sender_type="contact",
                            text="Está muy caro el Onix", created_at=t0),
                    Message(organization_id=1, conversation_id=conv.id, direction="out", sender_type="agent",
                            sender_agent_id=agent.id, text="Te ofrezco financiación a 60 meses",
                            created_at=t0 + timedelta(minutes=1)),
                ])
                await s.flush()
                conv.status, conv.typification_id = "closed", typ.id
        await s.commit()
        return luis.id, marta.id


async def test_memory_and_best_seller(client):
    c = client
    luis_id, marta_id = await _seed_sales()

    r = await c.post("/api/memory/runs", json={"typifications": ["Venta"], "max_conversations": 10})
    assert r.status_code == 200, r.text
    run = await wait_run(c, f"/api/memory/runs/{r.json()['id']}")
    assert run["status"] == "done", run["error"]
    # La base es compartida entre pruebas: puede haber otras ventas cerradas además de las de Luis y Marta
    assert run["stats"]["conversations"] >= 4 and run["stats"]["proposed"] == 2
    assert "Está muy caro el Onix" in llm_inputs[-1] and "Asesor Luis L" in llm_inputs[-1]

    items = (await c.get("/api/memory/items", params={"status": "pending"})).json()["items"]
    precio = next(i for i in items if i["title"] == "Precio alto")
    assert 999999 not in precio["evidence_conversation_ids"] and precio["evidence_conversation_ids"]

    # Segunda corrida: lo ya propuesto no se duplica
    run2 = await wait_run(c, f"/api/memory/runs/{(await c.post('/api/memory/runs', json={})).json()['id']}")
    assert run2["stats"]["proposed"] == 0 and run2["stats"]["duplicates"] >= 2

    approved = (await c.post(f"/api/memory/items/{precio['id']}/approve")).json()
    assert approved["status"] == "approved" and approved["reviewed_by"]
    horario = next(i for i in items if i["title"] == "Horario del taller")
    assert (await c.post(f"/api/memory/items/{horario['id']}/reject")).json()["status"] == "rejected"
    manual = await c.post("/api/memory/items", json={"kind": "policy", "title": "Garantía 3 años",
                                                     "content": "Garantía de fábrica de 3 años o 100.000 km."})
    assert manual.json()["status"] == "approved" and manual.json()["source"] == "manual"
    assert (await c.post("/api/memory/items", json={"kind": "policy", "title": "garantía 3 AÑOS",
                                                    "content": "x"})).status_code == 409
    counts = (await c.get("/api/memory/items")).json()["counts"]
    assert counts["approved"] >= 2 and counts["rejected"] >= 1
    async with SessionLocal() as s:
        approved_now = await learning.approved_memory(s, 1)
    assert {m.title for m in approved_now} >= {"Precio alto", "Garantía 3 años"}

    # Ranking: Luis primero (más ventas y mejor conversión)
    ranking = (await c.get("/api/seller/ranking")).json()
    top = {r["agent_id"]: r for r in ranking}
    order = [r["agent_id"] for r in ranking]
    assert order.index(luis_id) < order.index(marta_id)
    assert top[luis_id]["sales"] == 3 and top[luis_id]["conversion_pct"] == 75.0
    assert top[marta_id]["sales"] == 1

    assert (await c.post("/api/seller/runs", json={"agent_ids": [999999]})).status_code == 422
    r = await c.post("/api/seller/runs", json={"name": "Vendedor estrella", "agent_ids": [luis_id, marta_id]})
    assert r.status_code == 200, r.text
    for _ in range(50):
        run = next(x for x in (await c.get("/api/seller/runs")).json() if x["id"] == r.json()["id"])
        if run["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert run["status"] == "done", run["error"]["error"]
    assert "Garantía 3 años" not in llm_inputs[-1] and "Precio alto" in llm_inputs[-1]  # memoria de objeciones
    profiles = (await c.get("/api/seller/profiles")).json()
    profile = profiles[0]
    assert profile["name"] == "Vendedor estrella" and set(profile["source_agent_ids"]) == {luis_id, marta_id}
    assert "system_prompt" not in profile["playbook"] and profile["playbook"]["objections"]

    r = await c.put(f"/api/seller/profiles/{profile['id']}", json={"system_prompt": PLAYBOOK["system_prompt"] + " Sé breve."})
    assert r.json()["system_prompt"].endswith("Sé breve.")
    bot = (await c.post(f"/api/seller/profiles/{profile['id']}/create-agent")).json()
    assert bot["enabled"] is False and bot["system_prompt"].endswith("Sé breve.") and bot["name"] == "Vendedor estrella"
    assert (await c.get(f"/api/seller/profiles/{profile['id']}")).json()["status"] == "applied"
    revs = (await c.get("/api/revisions", params={"entity_type": "ai_agent", "entity_id": bot["id"]})).json()
    assert revs and revs[0]["source"] == "ai"


CSV = (
    "sku;nombre;descripcion;categoria;marca;precio;stock;año;color\n"
    "ONX-25;Chevrolet Onix Premier;Sedán compacto turbo, ideal para ciudad;Sedanes;Chevrolet;$ 82.990.000;3;2025;Rojo\n"
    "TRK-25;Chevrolet Tracker LT;Camioneta SUV con gran espacio;Camionetas;Chevrolet;105.000.000;0;2025;Gris\n"
    "SPK-24;Chevrolet Spark;Hatchback económico;Hatchback;Chevrolet;55.000.000;5;2024;Blanco\n"
    ";;sin nombre;;;;;;\n"
)


async def test_catalog_import_search_and_sync(client, monkeypatch):
    c = client
    r = await c.post("/api/catalog/import", files={"file": ("catalogo.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["invalid"]) == (3, 1)

    page = (await c.get("/api/catalog/products", params={"limit": 10})).json()
    onix = next(p for p in page["items"] if p["sku"] == "ONX-25")
    assert onix["price"] == 82990000 and onix["attributes"] == {"año": "2025", "color": "Rojo"}
    tracker = next(p for p in page["items"] if p["sku"] == "TRK-25")
    assert tracker["available"] is False  # stock 0
    assert "Camionetas" in page["categories"]

    # Excel y JSON actualizan por SKU
    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "Nombre", "Precio", "Stock", "Disponible"])
    ws.append(["TRK-25", "Chevrolet Tracker LT", 99_500_000, 2, "sí"])
    buf = io.BytesIO()
    wb.save(buf)
    r = await c.post("/api/catalog/import", files={"file": ("c.xlsx", buf.getvalue(),
                                                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.json()["updated"] == 1
    r = await c.post("/api/catalog/import", files={"file": ("c.json", json.dumps(
        [{"sku": "EQX-25", "name": "Chevrolet Equinox", "price": "150000000", "category": "Camionetas"}]).encode(),
        "application/json")})
    assert r.json()["created"] == 1

    async with SessionLocal() as s:
        # Texto completo en español (plural/singular, acentos) y orden por relevancia
        found = await catalog.search(s, 1, "camionetas suv")
        assert [p.sku for p in found][0] == "TRK-25"
        assert {p.sku for p in await catalog.search(s, 1, "sedán")} == {"ONX-25"}
        # Filtros de precio y categoría
        assert [p.sku for p in await catalog.search(s, 1, "chevrolet", max_price=60_000_000)] == ["SPK-24"]
        assert {p.sku for p in await catalog.search(s, 1, "", category="camion")} == {"TRK-25", "EQX-25"}
        # Sin coincidencia de texto completo: aproximada por nombre / SKU
        assert [p.sku for p in await catalog.search(s, 1, "Equinoxx")] == ["EQX-25"]
        assert [p.sku for p in await catalog.search(s, 1, "SPK")] == ["SPK-24"]
        desc = catalog.describe((await catalog.search(s, 1, "onix"))[0])
        assert desc.startswith("[ONX-25] Chevrolet Onix Premier | COP 82.990.000") and "año: 2025" in desc

    seen = (await c.get("/api/catalog/search", params={"q": "spark"})).json()
    assert seen[0]["as_seen_by_ai"].startswith("[SPK-24]")

    # CRUD manual
    r = await c.post("/api/catalog/products", json={"sku": "ONX-25", "name": "dup"})
    assert r.status_code == 409
    new = (await c.post("/api/catalog/products", json={"sku": "CAM-1", "name": "Chevrolet Captiva",
                                                         "price": 120000000})).json()
    assert new["currency"] == "COP" and new["source"] == "manual"
    assert (await c.put(f"/api/catalog/products/{new['id']}", json={
        "sku": "CAM-1", "name": "Chevrolet Captiva", "price": 119000000})).json()["price"] == 119000000

    # Feed externo: reemplaza su propia fuente (lo que no vino queda no disponible)
    assert (await c.put("/api/catalog/settings", json={"feed_url": "ftp://x"})).status_code == 422
    await c.put("/api/catalog/settings", json={"feed_url": "https://erp.example.com/feed.csv", "feed_format": "csv"})
    feeds = ["sku,nombre,precio\nF-1,Repuesto filtro,50000\nF-2,Aceite 5W30,80000\n", "sku,nombre,precio\nF-1,Repuesto filtro,52000\n"]

    class FakeHTTP:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            import httpx

            return httpx.Response(200, content=feeds.pop(0).encode(), request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog.httpx, "AsyncClient", FakeHTTP)
    assert (await c.post("/api/catalog/sync/feed")).json()["created"] == 2
    second = (await c.post("/api/catalog/sync/feed")).json()
    assert (second["updated"], second["disabled"]) == (1, 1)
    settings = (await c.get("/api/catalog/settings")).json()
    assert settings["last_sync"]["source"] == "feed"

    # Catálogo de Meta (cliente simulado)
    class FakeMeta:
        async def list_catalog_products(self, catalog_id):
            assert catalog_id == "CAT123"
            return [{"retailer_id": "M-1", "name": "Accesorio Meta", "price": "COP 30,000.00",
                     "availability": "in stock", "image_url": "https://img/1.jpg"}]

    async with SessionLocal() as s:
        with pytest.raises(ValueError):
            await catalog.sync_meta(s, 1, FakeMeta())
        await c.put("/api/catalog/settings", json={"meta_catalog_id": "CAT123"})
        result = await catalog.sync_meta(s, 1, FakeMeta())
        assert result["created"] == 1
        meta = (await catalog.search(s, 1, "accesorio"))[0]
        assert meta.in_meta_catalog and meta.source == "meta" and float(meta.price) == 30000

    runs = (await c.get("/api/catalog/sync-runs")).json()
    assert {r["source"] for r in runs} >= {"import", "feed", "meta"}
