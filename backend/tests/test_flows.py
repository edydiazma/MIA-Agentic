"""Flujos (Scratch Jr / Scratch 3): validación, ejecución con esperas y ramas, simulación, reanudación y seguridad."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db import SessionLocal
from app.flows.engine import resume_due
from app.models import ConversationEvent, FlowRun, utcnow
from app.whatsapp import WhatsAppClient
from tests.conftest import WA, FakeChat, settle, text

buttons_sent: list[tuple[str, str, list]] = []


@pytest.fixture(autouse=True)
def fake_buttons(monkeypatch):
    async def send_buttons(self, to, body, buttons):
        buttons_sent.append((to, body, [b["title"] for b in buttons]))
        return f"wamid.btn{len(buttons_sent)}"

    monkeypatch.setattr(WhatsAppClient, "send_buttons", send_buttons)


def b(id_, type_, inputs=None, **branches):
    block = {"id": id_, "type": type_, "inputs": inputs or {}}
    if branches:
        block["branches"] = branches
    return block


def definition(keyword: str, blocks: list) -> dict:
    return {"schema_version": 1, "variables": [{"name": "interes"}],
            "scripts": [{"id": "s1", "trigger": {"type": "keyword", "config": {"keywords": [keyword]}}, "blocks": blocks}]}


async def _new_flow(c, name: str, keyword: str, blocks: list) -> int:
    r = await c.post("/api/flows", json={"name": name, "trigger_type": "keyword",
                                         "trigger_config": {"keywords": [keyword]}, "editor_mode": "junior"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    r = await c.post(f"/api/flows/{fid}/versions", json={"definition": definition(keyword, blocks), "change_note": "v"})
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/flows/{fid}/publish", json={})
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    return fid


async def _conv(c, phone):
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_catalog_and_validation(client):
    c = client
    cat = (await c.get("/api/flows/catalog")).json()
    types = {blk["type"] for blk in cat["blocks"]}
    assert {"send_text", "wait_reply", "switch_reply", "ai_reply", "handoff", "http_request"} <= types
    assert any(blk["junior"] for blk in cat["blocks"]) and cat["categories"]

    r = await c.post("/api/flows", json={"name": "Inválido", "trigger_type": "keyword",
                                         "trigger_config": {"keywords": ["x"]}})
    fid = r.json()["id"]
    bad = definition("x", [b("a", "send_text", {}), b("a", "nope"), b("c", "http_request", {"method": "GET",
                                                                                          "url": "http://inseguro"})])
    r = await c.post(f"/api/flows/{fid}/versions", json={"definition": bad})
    assert r.status_code == 422
    messages = " | ".join(e["message"] for e in r.json()["detail"]["errors"])
    assert "Falta «Texto»" in messages and "Id repetido" in messages and "desconocido" in messages and "https" in messages
    # Un flujo sin bloques no se puede publicar
    assert (await c.post(f"/api/flows/{fid}/publish", json={})).status_code == 422


async def test_flow_buttons_wait_branch_and_close(client):
    c = client
    phone = "571440000001"
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Consulta resuelta", "Reclamo"]})
    blocks = [
        b("b1", "send_buttons", {"text": "Hola {{contact.first_name}}, ¿qué te interesa?",
                                 "buttons": ["Comprar", "Taller"]}),
        b("b2", "wait_reply", {"save_to": "interes", "timeout_min": 60}),
        b("b3", "switch_reply", {"options": ["Comprar", "Taller"]},
          **{"Comprar": [b("b4", "tag", {"tag": "venta"})],
             "Taller": [b("b5", "handoff_to_agent", {"reason": "Taller"})],
             "other": [b("b5b", "send_text", {"text": "No entendí"})]}),
        b("b6", "if", {"condition": {"op": "contains", "left": "{{vars.interes}}", "right": "comprar"}},
          then=[b("b7", "send_text", {"text": "Te envío la ficha, {{contact.first_name}}"})],
          **{"else": [b("b8", "send_text", {"text": "Ok"})]}),
        b("b9", "typify_close", {"typification": "Venta"}),
    ]
    fid = await _new_flow(c, "Promo flujo", "promoflujo", blocks)
    asked_ai = len(FakeChat.requests)

    await c.post("/webhooks/whatsapp", json=text(phone, "fl.1", "hola, vi la PROMOFLUJO", name="Ana Pérez"))
    await settle()
    assert buttons_sent[-1] == (phone, "Hola Ana, ¿qué te interesa?", ["Comprar", "Taller"])
    runs = (await c.get(f"/api/flows/{fid}/runs")).json()
    assert runs[0]["status"] == "waiting"

    await c.post("/webhooks/whatsapp", json=text(phone, "fl.2", "Comprar"))
    await settle()
    assert WA.sent[-1] == (phone, "Te envío la ficha, Ana")
    conv = await _conv(c, phone)
    assert conv["status"] == "closed" and conv["typification"] == "Venta" and "venta" in conv["tags"]
    assert len(FakeChat.requests) == asked_ai  # el flujo se encargó: el bot de IA no respondió

    run = (await c.get(f"/api/flows/{fid}/runs")).json()[0]
    assert run["status"] == "succeeded"
    detail = (await c.get(f"/api/flows/runs/{run['id']}")).json()
    assert [s["block_id"] for s in detail["steps"]] == ["b1", "b2", "b3", "b4", "b6", "b7", "b9"]
    assert detail["context"]["vars"]["interes"] == "Comprar"
    async with SessionLocal() as s:
        actors = (await s.execute(select(ConversationEvent.event_type, ConversationEvent.actor_type)
                                  .where(ConversationEvent.conversation_id == conv["id"]))).all()
    assert ("closed", "flow") in actors and ("tagged", "system") not in actors

    await c.post(f"/api/flows/{fid}/pause")


async def test_wait_time_resume_and_simulation(client):
    c = client
    phone = "571440000002"
    blocks = [b("w1", "send_text", {"text": "Ya te escribo"}), b("w2", "wait_time", {"minutes": 30}),
              b("w3", "set_var", {"name": "n", "value": 2}), b("w4", "change_var", {"name": "n", "delta": 3}),
              b("w5", "send_text", {"text": "Total {{vars.n}}"})]
    fid = await _new_flow(c, "Espera flujo", "esperaflujo", blocks)

    sim = (await c.post(f"/api/flows/{fid}/test", json={"text": "esperaflujo"})).json()
    assert sim["messages"] == [{"text": "Ya te escribo"}] and sim["waiting"] is True

    await c.post("/webhooks/whatsapp", json=text(phone, "fw.1", "esperaflujo"))
    await settle()
    assert WA.sent[-1] == (phone, "Ya te escribo")
    async with SessionLocal() as s:
        await s.execute(update(FlowRun).where(FlowRun.flow_id == fid, FlowRun.status == "waiting")
                        .values(resume_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert await resume_due() >= 1
    assert WA.sent[-1] == (phone, "Total 5.0")

    # En pausa ya no se dispara: responde el bot de IA
    await c.post(f"/api/flows/{fid}/pause")
    await c.post("/webhooks/whatsapp", json=text(phone, "fw.2", "esperaflujo otra vez"))
    await settle()
    assert WA.sent[-1] == (phone, "¡Hola! ¿En qué te ayudo?")


async def test_http_request_blocks_internal_network(client):
    c = client
    phone = "571440000003"
    fid = await _new_flow(c, "SSRF flujo", "ssrfflujo", [
        b("h1", "http_request", {"method": "GET", "url": "https://127.0.0.1/admin", "save_to": "r"}),
        b("h2", "send_text", {"text": "no debería llegar"})])
    await c.post("/webhooks/whatsapp", json=text(phone, "fh.1", "ssrfflujo"))
    await settle()
    run = (await c.get(f"/api/flows/{fid}/runs")).json()[0]
    assert run["status"] == "failed" and "red interna" in run["error"]
    assert WA.sent[-1][1] != "no debería llegar"
    await c.post(f"/api/flows/{fid}/pause")


async def test_switch_reply_waits_itself_and_repeat_body(client):
    c = client
    phone = "571440000004"
    fid = await _new_flow(c, "Menú flujo", "menuflujo", [
        b("m1", "send_text", {"text": "1) Precios 2) Horario"}),
        b("m2", "switch_reply", {"options": ["Precios", "Horario"], "timeout_min": 30},
          **{"Precios": [b("m3", "repeat", {"times": 2}, body=[b("m4", "change_var", {"name": "k", "delta": 1})]),
                         b("m5", "send_text", {"text": "Contador {{vars.k}}"})],
             "other": [b("m6", "send_text", {"text": "Opción no válida"})]}),
    ])
    await c.post("/webhooks/whatsapp", json=text(phone, "fm.1", "menuflujo"))
    await settle()
    assert WA.sent[-1] == (phone, "1) Precios 2) Horario")
    assert (await c.get(f"/api/flows/{fid}/runs")).json()[0]["status"] == "waiting"
    await c.post("/webhooks/whatsapp", json=text(phone, "fm.2", "1"))  # responde con el número de la opción
    await settle()
    assert WA.sent[-1] == (phone, "Contador 2.0")
    run = (await c.get(f"/api/flows/{fid}/runs")).json()[0]
    assert run["status"] == "succeeded"
    await c.post(f"/api/flows/{fid}/pause")


def test_catalog_parity():
    """El editor (respaldo) y el motor usan exactamente el mismo catálogo de bloques."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    backend = json.loads((root / "backend/app/flows/blocks.json").read_text(encoding="utf-8"))
    frontend = json.loads((root / "frontend/lib/flow-blocks.json").read_text(encoding="utf-8"))
    assert backend == frontend, "Copia frontend/lib/flow-blocks.json desde backend/app/flows/blocks.json"
