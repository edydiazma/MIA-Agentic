"""Agente avanzado (§17): origen del cliente, reglas por fuente (portales), etapas, seguridad, recuperación por
inactividad, tipificaciones con datos obligatorios y reactivación, y webhooks entrantes."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, select, update

from app import recovery
from app.ai import router as ai_router
from app.ai.base import AgentResult
from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import (
    AIAgent,
    Agent,
    Contact,
    ContactVehicle,
    Conversation,
    ConversationEvent,
    Deal,
    DealStageEvent,
    Message,
    Organization,
    PipelineStage,
    Typification,
    utcnow,
)
from tests.conftest import WA, FakeChat, settle, text
from tests.test_flows import _new_flow, b, fake_buttons  # noqa: F401  (fixture autouse del módulo de flujos)


async def _bot(c) -> dict:
    return (await c.get("/api/bots")).json()[0]


async def _conv(c, phone) -> dict:
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def _reset_bot(c, bot_id: int) -> None:
    r = await c.put(f"/api/bots/{bot_id}", json={
        "source_rules": [], "recovery_enabled": False, "recovery_attempts": [], "inactivity_end_hours": None,
        "security_enabled": True, "security_action": "close", "ad_context_prompt": None})
    assert r.status_code == 200, r.text


@pytest.fixture
def chat_tools(monkeypatch):
    """Cortex simulado que usa las herramientas set_stage / flag_security según el texto del cliente."""
    seen: list = []

    async def run_chat(session, cx, req, execute_tool, ctx):
        seen.append(req)
        last = " ".join(getattr(p, "text", "") for p in req.turns[-1].parts).lower() if req.turns else ""
        if "etapa mql" in last:
            await execute_tool("set_stage", {"stage": "nuevos:mql", "reason": "Modelo Onix y nombre entregado"})
            return AgentResult(text="Perfecto, ya tengo tus datos.")
        if "lo compro" in last:
            await execute_tool("set_stage", {"stage": "nuevos:won", "reason": "Confirmó la compra"})
            return AgentResult(text="¡Felicitaciones!")
        if "gana dinero" in last:
            await execute_tool("flag_security", {"kind": "spam", "reason": "Oferta de dinero fácil"})
            return AgentResult(text="no debería enviarse")
        return AgentResult(text="¡Hola! ¿En qué te ayudo?")

    monkeypatch.setattr(ai_router, "run_chat", run_chat)
    return seen


async def test_ad_context_and_portal_source_rule(client):
    c = client
    bot = await _bot(c)
    await c.post("/api/groups", json={"name": "Recuperación usados", "description": "Leads de portales"})
    r = await c.put(f"/api/bots/{bot['id']}", json={
        "ad_context_enabled": True, "ad_context_prompt": "Saluda mencionando el vehículo del anuncio.",
        "source_rules": [{"name": "Portales", "match": {"portal": "any"},
                          "actions": {"group": "Recuperación usados", "tags": ["portal"],
                                      "skip_intent_question": True}},
                         {"name": "Inválida", "match": {"channel": "google_ads"}, "actions": {}}]})
    assert r.status_code == 200, r.text
    assert r.json()["source_rules"][0]["actions"]["tags"] == ["portal"]
    bad = await c.put(f"/api/bots/{bot['id']}", json={"source_rules": [{"name": ""}]})
    assert bad.status_code == 422

    # 1) Anuncio de Meta: el primer mensaje recibe el bloque «Origen del cliente»
    phone = "573170000001"
    await c.post("/webhooks/whatsapp", json=text(phone, "ac.1", "hola", referral={
        "source_type": "ad", "source_id": "AD-77", "headline": "Isuzu Estacas precio de lanzamiento",
        "body": "Aprovecha el lanzamiento", "ctwa_clid": "CL1"}))
    await settle()
    ctx = FakeChat.requests[-1].context
    assert "## Origen del cliente" in ctx and "«Isuzu Estacas precio de lanzamiento»" in ctx
    assert "Saluda mencionando el vehículo del anuncio." in ctx
    await c.post("/webhooks/whatsapp", json=text(phone, "ac.2", "¿qué precio tiene?"))
    await settle()
    assert "## Origen del cliente" not in FakeChat.requests[-1].context  # solo en el primer mensaje

    # 2) Lead de portal (MercadoLibre): grupo de recuperación, etiqueta y no se pregunta el motivo
    phone2 = "573170000002"
    await c.post("/webhooks/whatsapp", json=text(phone2, "ac.3", "Hola, me interesa la camioneta publicada",
                                                 referral={"source_type": "post", "source_id": "P1", "ctwa_clid": "CL2",
                                                           "source_url": "https://articulo.mercadolibre.com.co/MCO-1"}))
    await settle()
    conv = await _conv(c, phone2)
    assert conv["group"] and conv["group"]["name"] == "Recuperación usados"
    assert "portal" in conv["tags"]
    ctx = FakeChat.requests[-1].context
    assert "MercadoLibre" in ctx and "No preguntes el motivo" in ctx
    async with SessionLocal() as s:
        ev = await s.scalar(select(ConversationEvent).where(
            ConversationEvent.conversation_id == conv["id"], ConversationEvent.event_type == "source_rule_applied"))
    assert ev and ev.payload["rule"] == "Portales" and ev.payload["portal"] == "mercadolibre"
    await _reset_bot(c, bot["id"])
    await c.delete(f"/api/groups/{conv['group']['id']}")  # la empresa de pruebas es compartida


async def test_stages_and_security_tools(client, chat_tools):
    c = client
    bot = await _bot(c)
    r = await c.post("/api/pipeline-stages/defaults", json={"industry": "automotriz"})
    assert r.status_code == 200, r.text
    nuevos = (await c.get("/api/pipelines/nuevos/stages")).json()
    assert [s["key"] for s in nuevos][:3] == ["lead", "mql", "sql"] and nuevos[-1]["is_lost"]
    assert "won" in [s["key"] for s in (await c.get("/api/deals/pipelines")).json()["pipelines"]["nuevos"]["stages"]]

    phone = "573170000003"
    await c.post("/webhooks/whatsapp", json=text(phone, "st.1", "quiero el onix, soy Ana. etapa mql"))
    await settle()
    req = chat_tools[-1]
    assert any(t.name == "set_stage" for t in req.tools) and "nuevos:mql" in req.context
    conv = await _conv(c, phone)
    async with SessionLocal() as s:
        deal = (await s.scalars(select(Deal).where(Deal.contact_id == conv["contact"]["id"]))).first()
        assert (deal.pipeline, deal.stage, deal.status) == ("nuevos", "mql", "open")
    await c.post("/webhooks/whatsapp", json=text(phone, "st.2", "listo, lo compro"))
    await settle()
    async with SessionLocal() as s:
        deal = await s.get(Deal, deal.id)
        events = (await s.scalars(select(DealStageEvent).where(DealStageEvent.deal_id == deal.id)
                                  .order_by(DealStageEvent.id))).all()
        assert deal.status == "won" and [e.to_stage for e in events] == ["mql", "won"]
        assert events[0].source == "ai" and "Onix" in events[0].reason
    hist = (await c.get(f"/api/deals/{deal.id}/stage-events")).json()
    assert len(hist) == 2

    # Seguridad: bloquear al remitente de spam
    await c.put(f"/api/bots/{bot['id']}", json={"security_enabled": True, "security_action": "block"})
    spam = "573170000004"
    await c.post("/webhooks/whatsapp", json=text(spam, "sec.1", "GANA DINERO desde casa, haz clic"))
    await settle()
    conv = await _conv(c, spam) if (await c.get("/api/conversations", params={"q": spam})).json() else None
    async with SessionLocal() as s:
        contact = await s.scalar(select(Contact).where(Contact.wa_id == spam))
        last_conv = (await s.scalars(select(Conversation).where(Conversation.contact_id == contact.id))).first()
        assert contact.blocked and last_conv.status == "closed" and last_conv.security_flag == "spam"
    assert WA.sent[-1][1].startswith("Por seguridad finalizamos")
    assert "no debería enviarse" not in [m for _p, m in WA.sent]
    await _reset_bot(c, bot["id"])
    assert conv is None or conv["id"] == last_conv.id
    async with SessionLocal() as s:  # sin etapas para las demás pruebas de la empresa compartida
        await s.execute(delete(PipelineStage).where(PipelineStage.organization_id == 1))
        await s.commit()


async def test_inactivity_recovery(client):
    c = client
    bot = await _bot(c)
    await c.post("/api/typifications", json={"name": "Sin respuesta (IA)", "section": "negative"})
    typ = next(t for t in (await c.get("/api/typifications/detailed")).json() if t["name"] == "Sin respuesta (IA)")
    r = await c.put(f"/api/bots/{bot['id']}", json={
        "recovery_enabled": True, "inactivity_end_hours": 6, "inactivity_end_typification_id": typ["id"],
        "recovery_attempts": [{"after_hours": 1, "message": "¿Sigues ahí? 😊 Tu Chevrolet te está esperando",
                               "use_ai": False},
                              {"after_hours": 3, "message": "Retoma con el modelo que le interesaba", "use_ai": True}]})
    assert r.status_code == 200, r.text
    assert (await c.put(f"/api/bots/{bot['id']}", json={"recovery_attempts": [
        {"after_hours": 13, "message": "x"}]})).status_code == 422

    phone = "573170000005"
    await c.post("/webhooks/whatsapp", json=text(phone, "rc.1", "hola, info del tracker"))
    await settle()
    conv_id = (await _conv(c, phone))["id"]

    async def tick(**shift):
        async with SessionLocal() as s:
            values = {k: utcnow() - timedelta(hours=v) for k, v in shift.items()}
            if values:
                await s.execute(update(Conversation).where(Conversation.id == conv_id).values(**values))
                await s.commit()
            conv = await s.get(Conversation, conv_id)
            agent = await s.get(AIAgent, bot["id"])
            return await recovery.process_conversation(s, conv, agent)

    assert await tick() is None  # aún no pasa 1 h
    assert await tick(last_inbound_at=2) == "attempt_1"
    assert WA.sent[-1] == (phone, "¿Sigues ahí? 😊 Tu Chevrolet te está esperando")
    assert await tick(last_recovery_at=1) is None  # el 2.º intento espera 3 h desde el 1.º
    n_req = len(FakeChat.requests)
    assert await tick(last_recovery_at=4) == "attempt_2"
    assert len(FakeChat.requests) == n_req + 1 and "Intento de recuperación 2" in FakeChat.requests[-1].context
    assert await tick(last_recovery_at=7) == "closed"
    conv = await _conv(c, phone)
    assert conv["status"] == "closed" and conv["typification"] == "Sin respuesta (IA)"

    # El cliente vuelve: se reinician los intentos; fuera de la ventana de 24 h no se escribe
    await c.post("/webhooks/whatsapp", json=text(phone, "rc.2", "sigo interesado"))
    await settle()
    async with SessionLocal() as s:
        assert (await s.get(Conversation, conv_id)).recovery_attempts_sent == 0
    sent_before = len(WA.sent)
    assert await tick(last_inbound_at=30) == "skipped_window"
    assert len(WA.sent) == sent_before
    assert await recovery.run_recovery() >= 0
    await _reset_bot(c, bot["id"])


async def test_typification_required_fields_and_reactivation(client):
    c = client
    r = await c.post("/api/typifications", json={"name": "Venta vehículo", "is_success": True, "section": "positive",
                                                 "keyword": "venta-veh",
                                                 "required_fields": ["deal.amount", "deal.currency"]})
    assert r.status_code == 200, r.text
    dup = await c.post("/api/typifications", json={"name": "Otra", "keyword": "VENTA-VEH"})
    assert dup.status_code == 409
    await c.post("/api/typifications", json={"name": "Pendiente cliente", "section": "followup",
                                             "reactivate_bot_after_h": 2})

    phone = "573170000006"
    await c.post("/webhooks/whatsapp", json=text(phone, "tp.1", "hola"))
    await settle()
    conv = await _conv(c, phone)
    await c.post(f"/api/conversations/{conv['id']}/assign")
    r = await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Venta vehículo"})
    assert r.status_code == 422 and set(r.json()["detail"]["missing"]) == {"deal.amount", "deal.currency"}
    r = await c.post(f"/api/conversations/{conv['id']}/close", json={
        "typification": "Venta vehículo", "values": {"deal.amount": 85000000, "deal.currency": "COP"}})
    assert r.status_code == 200, r.text

    # Seguimiento: el cliente responde antes de 2 h → vuelve al asesor, no al bot
    phone2 = "573170000007"
    await c.post("/webhooks/whatsapp", json=text(phone2, "tp.2", "hola"))
    await settle()
    conv2 = await _conv(c, phone2)
    await c.post(f"/api/conversations/{conv2['id']}/assign")
    assert (await c.post(f"/api/conversations/{conv2['id']}/close",
                         json={"typification": "Pendiente cliente"})).status_code == 200
    n_req = len(FakeChat.requests)
    await c.post("/webhooks/whatsapp", json=text(phone2, "tp.3", "ya revisé, sí me interesa"))
    await settle()
    conv2 = await _conv(c, phone2)
    assert conv2["status"] == "human" and conv2["assigned_agent"] is not None
    assert len(FakeChat.requests) == n_req  # el bot no respondió


async def test_inbound_webhook_send_template_and_flow(client):
    c = client
    channels = (await c.get("/api/channels")).json()["channels"]
    wa_channel = next(ch for ch in channels if ch.get("provider", "whatsapp_cloud") == "whatsapp_cloud")
    suggested = (await c.get("/api/inbound-webhooks/template-params", params={
        "channel_id": wa_channel["id"], "name": "promo", "language": "es"})).json()
    assert [p["maps_to"] for p in suggested] == ["recipient.phone", "contact.name", "template.body.1",
                                                 "template.body.2"]
    params = [
        {"name": "telefono", "type": "phone", "required": True, "maps_to": "recipient.phone", "example": "3001234567"},
        {"name": "nombre", "type": "text", "maps_to": "contact.name", "example": "Ana"},
        {"name": "saludo", "type": "text", "required": True, "maps_to": "template.body.1", "example": "Ana"},
        {"name": "oferta", "type": "text", "required": True, "maps_to": "template.body.2", "example": "tu cita"},
        {"name": "correo", "type": "email", "maps_to": "contact.email"},
        {"name": "placa", "type": "text", "maps_to": "vehicle.plate"},
        {"name": "marca", "type": "text", "maps_to": "vehicle.make"},
        {"name": "modelo", "type": "text", "maps_to": "vehicle.model"},
        {"name": "kilometraje", "type": "number", "maps_to": "vehicle.mileage_km"},
        {"name": "fecha_cita", "type": "date", "maps_to": "flow.var.fecha"},
        {"name": "cedula", "type": "text", "maps_to": "key:document"},
    ]
    r = await c.post("/api/inbound-webhooks", json={
        "name": "Confirmación Cita V.5", "action": "send_template", "channel_id": wa_channel["id"],
        "template_name": "promo", "template_language": "es", "params": params,
        "options": {"tags": ["cita-taller"], "bot": "keep"}})
    assert r.status_code == 200, r.text
    hook = r.json()
    token = hook["token"]
    assert token.startswith("whk_") and hook["status"] == "draft" and hook["params_count"] == 11
    assert hook["url"].endswith(f"/hooks/{hook['slug']}")
    assert (await c.get(f"/api/inbound-webhooks/{hook['id']}")).json()["token"] is None  # solo una vez
    bad = await c.post("/api/inbound-webhooks", json={"name": "Sin destinatario", "action": "send_template",
                                                      "template_name": "promo", "params": [
                                                          {"name": "x", "maps_to": "template.body.1"}]})
    assert bad.status_code == 422

    path = f"/hooks/{hook['slug']}"
    payload = {"telefono": "300 123 4567", "nombre": "Jhon Vanegas", "saludo": "Jhon", "oferta": "tu cita del 12/10",
               "correo": "Jhon@Mail.com", "placa": "abc-123", "marca": "Isuzu", "modelo": "D-Max",
               "kilometraje": "45.000", "fecha_cita": "12/10/2026", "cedula": "1020345678"}
    assert (await c.post(path, json=payload, headers={"X-Hook-Token": token})).status_code == 409  # borrador
    await c.post(f"/api/inbound-webhooks/{hook['id']}/publish")
    assert (await c.post(path, json=payload)).status_code == 401
    assert (await c.post(path, json=payload, headers={"X-Hook-Token": "whk_otro"})).status_code == 401
    r = await c.post(path, json={**payload, "oferta": ""}, headers={"X-Hook-Token": token})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["param"] == "oferta"

    n_tpl = len(WA.templates_sent)
    r = await c.post(path, json=payload, headers={"X-Hook-Token": token, "Idempotency-Key": "cita-991"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["message_id"] and out["contact_id"]
    to, name, components = WA.templates_sent[-1]
    assert (to, name) == ("573001234567", "promo") and len(WA.templates_sent) == n_tpl + 1
    assert [p["text"] for p in components[0]["parameters"]] == ["Jhon", "tu cita del 12/10"]
    async with SessionLocal() as s:
        contact = await s.get(Contact, out["contact_id"])
        assert contact.name == "Jhon Vanegas" and contact.email == "jhon@mail.com"
        v = await s.scalar(select(ContactVehicle).where(ContactVehicle.contact_id == contact.id))
        assert (v.plate, v.make, v.model, v.mileage_km) == ("ABC123", "Isuzu", "D-Max", 45000)
    conv = await _conv(c, "573001234567")
    assert "cita-taller" in conv["tags"]

    # Reintento con la misma Idempotency-Key: no se reenvía
    r = await c.post(path, json=payload, headers={"X-Hook-Token": token, "Idempotency-Key": "cita-991"})
    assert r.status_code == 200 and r.json()["duplicate"] and len(WA.templates_sent) == n_tpl + 1

    listed = next(h for h in (await c.get("/api/inbound-webhooks")).json() if h["id"] == hook["id"])
    assert (listed["executions"], listed["succeeded"], listed["failed"]) == (6, 1, 4)
    assert listed["created_by"]["name"] and listed["published_at"]
    runs = (await c.get(f"/api/inbound-webhooks/{hook['id']}/runs")).json()["items"]
    ok = next(r for r in runs if r["status"] == "succeeded")
    assert ok["payload"]["cedula"].endswith("5678") and ok["payload"]["cedula"].startswith("*")
    test = (await c.post(f"/api/inbound-webhooks/{hook['id']}/test")).json()
    assert test["ok"] and test["dry_run"] and "curl" in test

    # Acción «iniciar flujo» con variables del webhook
    fid = await _new_flow(c, "Bienvenida webhook", "nuncacoincide-hook",
                          [b("h1", "send_text", {"text": "Hola, tu asesor es {{vars.asesor}}"})])
    r = await c.post("/api/inbound-webhooks", json={
        "name": "Oferta usados", "action": "start_flow", "flow_id": fid,
        "params": [{"name": "telefono", "type": "phone", "required": True, "maps_to": "recipient.phone"},
                   {"name": "asesor", "type": "text", "required": True, "maps_to": "flow.var.asesor"}]})
    hook2 = r.json()
    await c.post(f"/api/inbound-webhooks/{hook2['id']}/publish")
    r = await c.post(f"/hooks/{hook2['slug']}?token={hook2['token']}",
                     json={"telefono": "573001112299", "asesor": "David Beltrán"})
    assert r.status_code == 200, r.text
    assert r.json()["flow_run_id"] and WA.sent[-1] == ("573001112299", "Hola, tu asesor es David Beltrán")
    await c.post(f"/api/flows/{fid}/pause")

    # Aislamiento: otra empresa no ve ni modifica el webhook
    async with SessionLocal() as s:
        if not await s.get(Organization, 9725):
            s.add(Organization(id=9725, name="Otra 9725", slug="otra-9725", timezone="America/Bogota"))
            await s.flush()
            s.add(Agent(organization_id=9725, email="admin9725@test.com", name="Admin 9725", role="admin",
                        password_hash=hash_password("secret9725")))
            await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as other:
        tok = (await other.post("/api/auth/login", json={"email": "admin9725@test.com",
                                                         "password": "secret9725"})).json()["access_token"]
        other.headers["Authorization"] = f"Bearer {tok}"
        assert (await other.get(f"/api/inbound-webhooks/{hook['id']}")).status_code == 404
        assert hook["id"] not in [h["id"] for h in (await other.get("/api/inbound-webhooks")).json()]
        assert (await other.post(f"/api/inbound-webhooks/{hook['id']}/rotate-token")).status_code == 404


async def test_messages_listing_unaffected():
    """El historial de mensajes del bot sigue registrando el remitente (sanidad del envío de plantillas)."""
    async with SessionLocal() as s:
        last = (await s.scalars(select(Message).where(Message.type == "template")
                                .order_by(Message.created_at.desc()).limit(1))).first()
    assert last is None or last.sender_type in ("bot", "agent", "campaign")


@pytest.fixture(autouse=True)
async def _cleanup_typifications():
    yield
    async with SessionLocal() as s:  # las tipificaciones creadas aquí no deben aparecer en otras pruebas
        await s.execute(update(Typification).where(Typification.organization_id == 1, Typification.name.in_(
            ["Sin respuesta (IA)", "Venta vehículo", "Pendiente cliente"])).values(is_active=False))
        await s.commit()
