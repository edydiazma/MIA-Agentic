"""Configuración como JSON (estilo n8n): exportar/importar y editar con IA, con validación, diff y revisiones."""

import json

import pytest
from sqlalchemy import select

from app.ai import router as ai_router
from app.ai.base import Usage
from app.db import SessionLocal
from app.models import Automation, Flow, FlowVersion

prompts: list[str] = []


def _edit(doc: dict, instruction: str) -> dict:
    """Simula al LLM: aplica la instrucción al documento."""
    doc = json.loads(json.dumps(doc))
    i = instruction.lower()
    if "sin catálogo" in i:
        doc["use_catalog"] = False
        doc["handoff_message"] = "Ya te paso con un asesor humano."
    if "citas de 45" in i:
        doc.update(enabled=True, duration_min=45)
    if "latencia" in i:
        doc.update(strategy="lowest_latency", max_latency_ms=1500)
    if "saludo" in i:
        doc["scripts"].append({"id": "s1", "trigger": {"type": "inbound_message"},
                               "blocks": [{"type": "send_text", "text": "¡Hola!"}]})
    if "palabra clave" in i:
        doc["config"]["keywords"].append("garantia")
    if "rompe" in i:
        doc["strategy"] = "al azar"
    return doc


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    async def complete_json(conn, system, user, schema, max_tokens=4000):
        prompts.append(system + "\n" + user)
        assert "document_json" in schema["properties"]
        instruction = user.split("\n", 1)[0].removeprefix("Instrucción: ")
        current = json.loads(user.split("):\n", 1)[1])
        return {"document_json": json.dumps(_edit(current, instruction)), "summary": "Listo"}, Usage(50, 30)

    monkeypatch.setattr(ai_router, "_complete_json", complete_json)


async def propose(c, entity_type, entity_id, instruction):
    r = await c.post("/api/ai/json-edit", json={"entity_type": entity_type, "entity_id": str(entity_id),
                                                "instruction": instruction})
    assert r.status_code == 200, r.text
    return r.json()


async def apply(c, p, instruction):
    return await c.post("/api/ai/json-edit/apply", json={"entity_type": p["entity_type"], "entity_id": p["entity_id"],
                                                         "document": p["proposal"], "instruction": instruction})


async def test_edit_agent_setting_and_cortex_with_ai(client):
    c = client
    bot = (await c.get("/api/bots")).json()[0]

    instr = "Deja este agente sin catálogo y cambia el mensaje de transferencia"
    p = await propose(c, "ai_agent", bot["id"], instr)
    assert p["valid"] and p["summary"] == "Listo"
    assert {d["path"] for d in p["diff"]} == {"use_catalog", "handoff_message"}
    assert p["ai_call_ids"] and p["attempts"][0]["status"] == "ok"
    assert "JSON Schema" in prompts[-1] and '"system_prompt"' in prompts[-1]
    # Nada cambió hasta aplicar
    assert (await c.get(f"/api/bots/{bot['id']}")).json()["use_catalog"] is True

    assert (await c.post("/api/ai/json-edit/apply", json={"entity_type": "ai_agent", "entity_id": str(bot["id"]),
                                                          "document": p["proposal"]})).status_code == 422
    r = await apply(c, p, instr)
    assert r.status_code == 200, r.text
    assert r.json()["document"]["use_catalog"] is False
    saved = (await c.get(f"/api/bots/{bot['id']}")).json()
    assert saved["use_catalog"] is False and saved["handoff_message"] == "Ya te paso con un asesor humano."
    revs = (await c.get("/api/revisions", params={"entity_type": "ai_agent", "entity_id": bot["id"]})).json()
    assert revs[0]["source"] == "ai" and revs[0]["ai_prompt"] == instr

    # Restaurar la revisión anterior (si existe una humana previa) o volver a la primera
    first = revs[-1]
    full = (await c.get(f"/api/revisions/{first['id']}")).json()
    assert full["document"]["name"] == bot["name"]

    # Ajustes: citas
    instr = "Habilita citas de 45 minutos"
    p = await propose(c, "setting", "appointments", instr)
    assert {d["path"] for d in p["diff"]} == {"enabled", "duration_min"}
    assert (await apply(c, p, instr)).status_code == 200
    doc = (await c.get("/api/ai/json-edit/document", params={"entity_type": "setting",
                                                            "entity_id": "appointments"})).json()
    assert doc["document"]["duration_min"] == 45 and doc["schema"]["type"] == "object"
    revs = (await c.get("/api/revisions", params={"entity_type": "setting", "entity_id": "appointments"})).json()
    assert revs[0]["ai_prompt"] == instr and revs[0]["source"] == "ai"

    # Cortex: propuesta inválida se reporta y no se puede aplicar
    cx = (await c.get("/api/ai/cortexes")).json()[0]
    bad = await propose(c, "cortex", cx["id"], "Rompe la estrategia")
    assert bad["valid"] is False and any("strategy" in e for e in bad["errors"])
    r = await apply(c, bad, "Rompe la estrategia")
    assert r.status_code == 422
    good = await propose(c, "cortex", cx["id"], "Prioriza la menor latencia")
    assert (await apply(c, good, "Prioriza la menor latencia")).status_code == 200
    cx_now = (await c.get(f"/api/ai/cortexes/{cx['id']}")).json()
    assert cx_now["strategy"] == "lowest_latency" and cx_now["max_latency_ms"] == 1500
    assert [m["connection_id"] for m in cx_now["members"]] == [m["connection_id"] for m in cx["members"]]

    # Entidad desconocida
    r = await c.post("/api/ai/json-edit", json={"entity_type": "nada", "entity_id": "1", "instruction": "x"})
    assert r.status_code == 422


async def test_flow_and_automation_documents(client):
    c = client
    async with SessionLocal() as s:
        flow = Flow(organization_id=1, name="Bienvenida JSON", trigger_type="inbound_message")
        auto = Automation(organization_id=1, name="Garantías JSON", type="keyword_handoff",
                          config={"keywords": ["reclamo"], "match": "contains"})
        s.add_all([flow, auto])
        await s.commit()
        flow_id, auto_id = flow.id, auto.id

    # Flujo: cada aplicación crea una versión inmutable que pasa a ser la actual
    instr = "Agrega un saludo al recibir mensajes"
    p = await propose(c, "flow", flow_id, instr)
    assert p["current"] == {"schema_version": 1, "variables": {}, "scripts": []}
    assert (await apply(c, p, instr)).status_code == 200
    p2 = await propose(c, "flow", flow_id, instr)
    assert (await apply(c, p2, instr)).status_code == 200
    async with SessionLocal() as s:
        versions = (await s.scalars(select(FlowVersion).where(FlowVersion.flow_id == flow_id)
                                    .order_by(FlowVersion.version))).all()
        flow = await s.get(Flow, flow_id)
        assert [v.version for v in versions] == [1, 2] and all(v.created_by_ai for v in versions)
        assert flow.current_version_id == versions[-1].id and versions[-1].ai_prompt == instr
        assert len(versions[-1].definition["scripts"]) == 2

    # Automatización
    instr = "Agrega la palabra clave garantia"
    p = await propose(c, "automation", auto_id, instr)
    assert p["diff"] == [{"path": "config/keywords", "before": ["reclamo"], "after": ["reclamo", "garantia"],
                          "change": "changed"}]
    assert (await apply(c, p, instr)).status_code == 200

    # Importar a mano (estilo n8n): mismas validaciones, revisión "import"
    doc = (await c.get("/api/ai/json-edit/document", params={"entity_type": "automation", "entity_id": auto_id})).json()
    doc["document"]["enabled"] = False
    r = await c.put("/api/ai/json-edit/document", json={"entity_type": "automation", "entity_id": str(auto_id),
                                                        "document": doc["document"]})
    assert r.status_code == 200 and r.json()["diff"][0]["path"] == "enabled"
    bad = {**doc["document"], "type": "inexistente"}
    assert (await c.put("/api/ai/json-edit/document", json={"entity_type": "automation", "entity_id": str(auto_id),
                                                            "document": bad})).status_code == 422
    revs = (await c.get("/api/revisions", params={"entity_type": "automation", "entity_id": auto_id})).json()
    assert [r["source"] for r in revs] == ["import", "ai"]

    # Restaurar la revisión de la IA
    r = await c.post(f"/api/revisions/{revs[1]['id']}/restore")
    assert r.status_code == 200 and r.json()["document"]["enabled"] is True
