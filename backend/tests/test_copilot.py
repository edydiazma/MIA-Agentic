"""Copiloto del asesor y asistente del supervisor (§19.3), en una empresa aislada y con el modelo simulado."""

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import hash_password
from app.copilot import core, guard, hooks, llm
from app.db import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentGroup,
    AssistantMessage,
    Channel,
    Contact,
    Conversation,
    CopilotSuggestion,
    Group,
    InteractionProduct,
    Message,
    Organization,
    utcnow,
)
from app.realtime import hub
from app.settings_store import set_setting

ORG = 9520
PWD = "Copiloto-9520!"
IDS: dict = {}
CALLS: list[tuple[str, str]] = []
PUSHED: list[tuple[int, str, dict]] = []


async def fake_call_json(org, feature, system, user, schema, conversation_id=None, max_tokens=1500):
    CALLS.append((feature, user))
    props = schema.get("properties", {})
    if "suggestions" in props:
        null_payload = {k: None for k in props["next_action"]["properties"]["payload"]["required"]}
        return {"suggestions": [{"text": "¡Hola! Claro, te cuento las versiones disponibles del catálogo."},
                                {"text": "Ese modelo cuesta $ 99.999.999 con descuento."},   # cifra inventada
                                {"text": "¿Para cuándo piensas comprar?"}],
                "next_action": {"action": "send_product", "label": "Enviar ficha del Onix",
                                "payload": {**null_payload, "sku": "ONIX-1", "product_name": "Onix LTZ",
                                            "text": "Te comparto la ficha del Onix LTZ."},
                                "reason": "Preguntó por el modelo", "confidence": 0.8}}, 111, 42
    if "need" in props:
        return {"need": "Cotizar un Onix", "captured_data": ["Nombre: Ana"], "bot_promises": ["Llamada hoy"],
                "sentiment": "positive", "next_step": "Enviar cotización"}, 112, 30
    if "summary" in props:
        return {"summary": "Ana pidió cotizar un Onix; se le envió la ficha.", "outcome": "Cotización enviada"}, 113, 30
    if "rebaja" in user:  # reescritura que intenta agregar una cifra que no estaba
        return {"text": "Te lo dejo en $ 50.000.000"}, 114, 20
    return {"text": "Hola Ana, ¿te sirve una cita el jueves a las 10?"}, 115, 25


@pytest.fixture(autouse=True)
def fake_copilot(monkeypatch):
    CALLS.clear()
    PUSHED.clear()
    monkeypatch.setattr(llm, "call_json", fake_call_json)

    async def send_to_agent(org, agent_id, event, data):
        PUSHED.append((agent_id, event, data))

    monkeypatch.setattr(hub, "send_to_agent", send_to_agent)


async def _seed() -> dict:
    if IDS:
        return IDS
    async with SessionLocal() as s:
        s.add(Organization(id=ORG, name="Concesionario Copiloto", slug="org-copiloto", timezone="America/Bogota",
                           onboarding_completed_at=utcnow()))
        await s.flush()
        ga, gb = Group(organization_id=ORG, name="Nuevos"), Group(organization_id=ORG, name="Taller")
        s.add_all([ga, gb])
        users = {k: Agent(organization_id=ORG, email=f"{k}@cop9520.co", name=n, role=r,
                          password_hash=hash_password(PWD))
                 for k, n, r in (("admin", "Admin Copiloto", "admin"), ("sup", "Sara Supervisora", "supervisor"),
                                 ("a1", "Andrés Asesor", "agent"), ("b1", "Beto Taller", "agent"))}
        s.add_all(users.values())
        ch = Channel(organization_id=ORG, name="WA 9520", phone_number_id="PN9520")
        s.add(ch)
        await s.flush()
        s.add_all([AgentGroup(agent_id=users["sup"].id, group_id=ga.id, role="supervisor"),
                   AgentGroup(agent_id=users["a1"].id, group_id=ga.id),
                   AgentGroup(agent_id=users["b1"].id, group_id=gb.id)])
        ana = Contact(organization_id=ORG, wa_id="573019520001", name="Ana Ruiz")
        bot_contact = Contact(organization_id=ORG, wa_id="573019520002", name="Luis Bot")
        s.add_all([ana, bot_contact])
        await s.flush()
        human = Conversation(organization_id=ORG, contact_id=ana.id, channel_id=ch.id, group_id=ga.id, status="human",
                             assigned_agent_id=users["a1"].id)
        bot = Conversation(organization_id=ORG, contact_id=bot_contact.id, channel_id=ch.id, status="bot")
        other = Conversation(organization_id=ORG, contact_id=bot_contact.id, channel_id=ch.id, group_id=gb.id,
                             status="human", assigned_agent_id=users["b1"].id)
        s.add_all([human, bot, other])
        await s.flush()
        s.add_all([
            Message(organization_id=ORG, conversation_id=human.id, direction="in", sender_type="contact",
                    text="Hola, quiero información del Onix"),
            Message(organization_id=ORG, conversation_id=human.id, direction="out", sender_type="bot",
                    text="¡Hola Ana! Te paso con un asesor."),
            Message(organization_id=ORG, conversation_id=bot.id, direction="in", sender_type="contact", text="Hola"),
        ])
        await set_setting(s, "copilot", {"debounce_seconds": 0}, ORG)
        await s.commit()
        IDS.update({k: u.id for k, u in users.items()}, human=human.id, bot=bot.id, other=other.id, ga=ga.id,
                   gb=gb.id, channel=ch.id, ana=ana.id)
    return IDS


async def _login(who: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": f"{who}@cop9520.co", "password": PWD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


async def _customer_says(conv_id: int, text: str) -> int:
    async with SessionLocal() as s:
        m = Message(organization_id=ORG, conversation_id=conv_id, direction="in", sender_type="contact", text=text)
        s.add(m)
        await s.commit()
        return m.id


def test_guard_rejects_unsupported_prices_and_availability():
    ctx = "Catálogo: [ONIX-1] Onix LTZ | COP 85.900.000"
    assert guard.check("El Onix LTZ está en $ 85.900.000", ctx, True) == []
    assert guard.check("Te lo dejo en 80 millones", ctx, True)
    assert guard.check("Sí, lo tenemos disponible para entrega", "", False)
    assert core.edit_distance("Hola Ana", "hola  ana") == 0 and core.edit_distance("Hola", "Hola!") == 1


async def test_suggestions_on_inbound_outcome_draft_rewrite_and_action():
    ids = await _seed()
    a1 = await _login("a1")
    msg_id = await _customer_says(ids["human"], "¿Cuánto cuesta el Onix?")
    hooks.on_inbound(ids["human"], msg_id)
    await hooks.drain()

    r = await a1.get(f"/api/conversations/{ids['human']}/copilot")
    assert r.status_code == 200, r.text
    data = r.json()
    reply = data["reply"]
    assert reply["message_id"] == msg_id and reply["kind"] == "reply" and data["fresh"]
    assert len(reply["content"]["options"]) == 2  # la del precio inventado se descartó
    assert all("99.999.999" not in o for o in reply["content"]["options"])
    assert reply["content"]["rejected"][0]["flags"]
    assert data["next_action"]["content"]["action"] == "send_product"
    assert any(p[0] == ids["a1"] and p[1] == "copilot.suggestion" for p in PUSHED)  # al asesor asignado

    # Caché por mensaje: pedir de nuevo sin fuerza no vuelve a llamar al modelo
    async with SessionLocal() as s:
        conv = await s.get(Conversation, ids["human"])
        again = await core.generate(s, conv)
    assert again.get("cached") and sum(1 for f, _ in CALLS if f == "reply") == 1

    # Resultado: aceptada tal cual / editada (distancia)
    sid = reply["id"]
    first = reply["content"]["options"][0]
    r = await a1.post(f"/api/copilot/suggestions/{sid}/outcome", json={"status": "accepted", "final_text": first})
    assert r.json()["status"] == "accepted" and r.json()["edit_distance"] == 0
    r = await a1.post(f"/api/copilot/suggestions/{sid}/outcome",
                      json={"status": "accepted", "final_text": first + " Saludos."})
    assert r.json()["status"] == "edited" and r.json()["edit_distance"] > 0
    assert (await a1.post(f"/api/copilot/suggestions/{sid}/outcome", json={"status": "x"})).status_code == 422

    # Siguiente acción: enviar producto → queda registrado como cotizado y devuelve el texto a insertar
    nid = data["next_action"]["id"]
    r = await a1.post(f"/api/copilot/suggestions/{nid}/execute")
    assert r.status_code == 200, r.text
    assert r.json()["insert_text"] == "Te comparto la ficha del Onix LTZ."
    async with SessionLocal() as s:
        prod = (await s.scalars(select(InteractionProduct).where(
            InteractionProduct.conversation_id == ids["human"]))).first()
    assert prod is not None and prod.stage == "quoted" and prod.source == "agent"

    # Borrador y reescritura
    r = await a1.post(f"/api/conversations/{ids['human']}/copilot/draft", json={"instruction": "ofrece cita el jueves"})
    assert r.status_code == 200 and "jueves" in r.json()["content"]["text"]
    r = await a1.post("/api/copilot/rewrite", json={"text": "hola ana tu carro esta listo", "mode": "fix_grammar",
                                                   "conversation_id": ids["human"]})
    assert r.status_code == 200 and r.json()["flags"] == []
    r = await a1.post("/api/copilot/rewrite", json={"text": "dale una rebaja", "mode": "friendlier"})
    assert r.json()["flags"] and r.json()["text"] == "dale una rebaja"  # cifra nueva: se conserva el original
    assert (await a1.post("/api/copilot/rewrite", json={"text": "x", "mode": "poema"})).status_code == 422
    await a1.aclose()


async def test_only_human_conversations_debounce_and_limits():
    ids = await _seed()
    started = utcnow()  # solo cuenta lo que genera esta prueba (otras pruebas del archivo usan las mismas conversaciones)
    async with SessionLocal() as s:
        bot = await s.get(Conversation, ids["bot"])
        assert (await core.generate(s, bot)) == {"skipped": "not_human"}
    hooks.on_inbound(ids["bot"], 0)
    await hooks.drain()
    async with SessionLocal() as s:
        n_bot = await s.scalar(select(func.count()).select_from(CopilotSuggestion).where(
            CopilotSuggestion.conversation_id == ids["bot"], CopilotSuggestion.created_at >= started))
    assert n_bot == 0

    # Antirrebote: una ráfaga de 3 mensajes → una sola generación
    hooks.DELAY_OVERRIDE = 0.2
    try:
        for t in ("hola", "¿siguen ahí?", "necesito la cotización"):
            hooks.on_inbound(ids["human"], await _customer_says(ids["human"], t))
        await hooks.drain()
    finally:
        hooks.DELAY_OVERRIDE = 0
    assert sum(1 for f, _ in CALLS if f == "reply") == 1

    # Límite por hora (las forzadas tienen el doble)
    async with SessionLocal() as s:
        await set_setting(s, "copilot", {"max_per_conversation_per_hour": 1}, ORG)
        await s.commit()
    a1 = await _login("a1")
    results = [(await a1.post(f"/api/conversations/{ids['human']}/copilot/refresh")).json() for _ in range(4)]
    assert results[-1] == {"skipped": "rate_limited"}
    async with SessionLocal() as s:
        await set_setting(s, "copilot", {"max_per_conversation_per_hour": 12}, ORG)
        await s.commit()
    await a1.aclose()


async def test_handoff_and_close_summaries():
    ids = await _seed()
    from app.service import close, handoff

    async with SessionLocal() as s:
        conv = await s.get(Conversation, ids["bot"])
        await handoff(s, conv, "pidió asesor")
    await hooks.drain()
    async with SessionLocal() as s:
        conv = await s.get(Conversation, ids["bot"])
        assert "Necesidad: Cotizar un Onix" in conv.handoff_summary and "Llamada hoy" in conv.handoff_summary
        await close(s, conv, None)
    await hooks.drain()
    async with SessionLocal() as s:
        conv = await s.get(Conversation, ids["bot"])
        assert conv.summary.startswith("Ana pidió cotizar")
    admin = await _login("admin")
    r = await admin.post(f"/api/conversations/{ids['bot']}/summary")
    assert r.status_code == 200 and r.json()["summary"]
    await admin.aclose()


async def test_assistant_scope_and_adoption_report(monkeypatch, client):
    ids = await _seed()

    async def fake_chat(session, org, req, execute_tool, conversation_id=None):
        from app.ai.base import AgentResult

        await execute_tool("service_kpis", {"group_id": ids["gb"]})  # grupo que no supervisa
        await execute_tool("service_kpis", {})
        await execute_tool("copilot_adoption", {})
        return AgentResult(text="En los últimos 7 días tu equipo atendió los casos asignados."), 999

    monkeypatch.setattr(llm, "chat", fake_chat)
    sup = await _login("sup")
    r = await sup.post("/api/assistant/ask", json={"text": "¿Cómo va el nivel de servicio?"})
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    answer = msgs[-1]
    assert answer["role"] == "assistant" and answer["content"].startswith("En los últimos")
    calls = answer["tool_calls"]
    assert calls[0] == {"name": "service_kpis", "input": {"group_id": ids["gb"]}, "ok": False}  # fuera de alcance
    assert calls[1]["ok"] and calls[2]["ok"]
    thread_id = r.json()["thread"]["id"]
    detail = (await sup.get(f"/api/assistant/threads/{thread_id}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "tool", "assistant"]

    a1 = await _login("a1")
    assert (await a1.post("/api/assistant/ask", json={"text": "hola"})).status_code == 403
    assert (await a1.get(f"/api/assistant/threads/{thread_id}")).status_code == 403

    # Otra empresa no ve la conversación ni el hilo
    other = client  # administrador de la empresa 1
    assert (await other.get(f"/api/conversations/{ids['human']}/copilot")).status_code == 404
    assert (await other.get(f"/api/assistant/threads/{thread_id}")).status_code == 404

    # Supervisor del grupo Nuevos no ve el copiloto de una conversación de Taller
    assert (await sup.get(f"/api/conversations/{ids['other']}/copilot")).status_code == 404

    # Reporte de adopción: datos de la empresa (sugerencias de las pruebas anteriores)
    admin = await _login("admin")
    # Al menos una sugerencia propia (no depende de que otra prueba del archivo haya generado alguna)
    async with SessionLocal() as s:
        s.add(CopilotSuggestion(organization_id=ORG, conversation_id=ids["human"], kind="reply",
                                content={"text": "Hola"}, status="shown", latency_ms=500))
        await s.commit()
    rep = (await admin.get("/api/reports/copilot")).json()
    assert rep["totals"]["shown"] >= 1 and "by_agent" in rep and rep["series"]
    async with SessionLocal() as s:
        n = await s.scalar(select(func.count()).select_from(AssistantMessage))
    assert n >= 3
    for c in (sup, a1, admin):
        await c.aclose()
