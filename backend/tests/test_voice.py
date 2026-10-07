"""Llamadas de WhatsApp (Calling API): rechazo, contestar desde el navegador, fin, resumen, agente de voz y métricas.
Meta y el runtime de medios están simulados (sin aiortc ni OpenAI reales)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.ai import router as ai_router
from app.db import SessionLocal
from app.models import Call, Channel, ConversationEvent, Message, utcnow
from app.realtime import hub
from app.voice import agent_runtime
from app.voice import calls as signaling
from app.voice.meta import CallingClient
from tests.conftest import settle

OFFER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
actions: list[tuple[str, str, str | None]] = []
settings_sent: list[tuple[bool, dict | None]] = []
events: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def fake_meta(monkeypatch):
    async def action(self, call_id, action, sdp=None, callback_data=None):
        actions.append((call_id, action, sdp))
        return {"success": True}

    async def set_calling(self, enabled, hours, tz):
        settings_sent.append((enabled, hours))
        return {"success": True}

    original = hub.broadcast

    async def broadcast(event, data, organization_id=None):
        events.append((event, data))
        await original(event, data, organization_id)

    monkeypatch.setattr(CallingClient, "action", action)
    monkeypatch.setattr(CallingClient, "set_calling", set_calling)
    monkeypatch.setattr(hub, "broadcast", broadcast)


def connect(call_id: str, wa_id: str, name: str = "Ana") -> dict:
    return {"entry": [{"id": "WABA", "changes": [{"field": "calls", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "calls": [{"id": call_id, "from": wa_id, "to": "57300", "event": "connect", "timestamp": "1",
                   "direction": "USER_INITIATED", "session": {"sdp_type": "offer", "sdp": OFFER}}]}}]}]}


def terminate(call_id: str, wa_id: str, status: str = "COMPLETED") -> dict:
    return {"entry": [{"id": "WABA", "changes": [{"field": "calls", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
        "calls": [{"id": call_id, "from": wa_id, "event": "terminate", "direction": "USER_INITIATED",
                   "status": [status], "duration": 120}]}}]}]}


async def _channel_id() -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(Channel.id).where(Channel.phone_number_id == "PNID"))


async def _call(wa_call_id: str) -> Call:
    async with SessionLocal() as s:
        return await s.scalar(select(Call).where(Call.wa_call_id == wa_call_id))


async def test_calling_disabled_rejects(client):
    c = client
    await c.put(f"/api/voice/channels/{await _channel_id()}", json={"calling_enabled": False, "sync_meta": False})
    await c.post("/webhooks/whatsapp", json=connect("wacid.off", "571550000001"))
    await settle()
    call = await _call("wacid.off")
    assert call.status == "rejected" and "desactivadas" in call.end_reason
    assert ("wacid.off", "reject", None) in actions


async def test_human_answers_in_browser(client, monkeypatch):
    c = client
    me = (await c.get("/api/auth/me")).json()
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: {me["id"]})

    async def fake_json_call(org, cortex_id, purpose, system, user, schema, conversation_id=None, max_tokens=4000):
        assert "Cliente: Quiero cambiar mi cita" in user
        return {"summary": "El cliente pidió cambiar su cita al viernes."}, None

    monkeypatch.setattr(ai_router, "json_call", fake_json_call)

    r = await c.put(f"/api/voice/channels/{await _channel_id()}", json={
        "calling_enabled": True, "calling_hours": None, "voice_agent_id": None, "sync_meta": True})
    assert r.status_code == 200, r.text
    assert settings_sent[-1] == (True, None)

    await c.post("/webhooks/whatsapp", json=connect("wacid.human", "571550000002", "Beto"))
    await settle()
    incoming = [d for e, d in events if e == "call.incoming" and d["wa_call_id"] == "wacid.human"]
    assert incoming and incoming[0]["sdp_offer"] == OFFER and incoming[0]["targets"] == [me["id"]]
    active = (await c.get("/api/calls/active")).json()
    assert any(a["wa_call_id"] == "wacid.human" and a["sdp_offer"] == OFFER for a in active)

    call_id = incoming[0]["id"]
    r = await c.post(f"/api/calls/{call_id}/answer", json={"sdp": "v=0 browser-answer"})
    assert r.status_code == 200 and r.json()["status"] == "connected" and r.json()["handled_by"] == "agent"
    assert ("wacid.human", "pre_accept", "v=0 browser-answer") in actions
    assert ("wacid.human", "accept", "v=0 browser-answer") in actions
    assert (await c.post(f"/api/calls/{call_id}/answer", json={"sdp": "x"})).status_code == 409

    await signaling.add_turn(call_id, "contact", "Quiero cambiar mi cita")
    async with SessionLocal() as s:  # 2 min 5 s de conversación
        await s.execute(update(Call).where(Call.id == call_id).values(answered_at=utcnow() - timedelta(seconds=125)))
        await s.commit()
    await c.post("/webhooks/whatsapp", json=terminate("wacid.human", "571550000002"))
    await settle(0.6)

    detail = (await c.get(f"/api/calls/{call_id}")).json()
    assert detail["status"] == "ended" and 124 <= detail["duration_s"] <= 126
    assert detail["summary"] == "El cliente pidió cambiar su cita al viernes."
    assert detail["turns"][0]["speaker"] == "contact"
    assert all("sdp_offer" not in e["payload"] for e in detail["events"])
    async with SessionLocal() as s:
        card = await s.scalar(select(Message).where(Message.conversation_id == detail["conversation_id"],
                                                    Message.type == "call"))
        assert card and "un asesor · 2:0" in card.text
        kinds = set((await s.scalars(select(ConversationEvent.event_type).where(
            ConversationEvent.conversation_id == detail["conversation_id"]))).all())
        assert {"call_started", "call_ended"} <= kinds
        minutes = await s.scalar(text("select value from usage_counters where organization_id = 1 "
                                      "and metric = 'voice_minutes'"))
        assert float(minutes) >= 3
    assert (await c.get(f"/api/calls/{call_id}/recording")).status_code == 404


async def test_voice_agent_answers_and_hangup(client, monkeypatch):
    c = client
    stopped = []

    class FakeMedia:
        async def start(self, sdp_offer):
            assert sdp_offer == OFFER
            return "v=0 bot-answer"

        async def stop(self):
            stopped.append(True)

        async def bridge_to_agent(self, offer):
            return "v=0 bridge"

    async def factory(call, va, instructions):
        assert "## Llamada telefónica" in instructions
        return FakeMedia()

    monkeypatch.setattr(agent_runtime, "runtime_available", lambda: True)
    monkeypatch.setattr(agent_runtime, "create_media_session", factory)

    bots = (await c.get("/api/bots")).json()
    va = await c.post("/api/voice/agents", json={"name": "Recepción", "ai_agent_id": bots[0]["id"],
                                                 "greeting": "Hola, ¿en qué te ayudo?"})
    assert va.status_code == 200, va.text
    await c.put(f"/api/voice/channels/{await _channel_id()}", json={
        "calling_enabled": True, "voice_agent_id": va.json()["id"], "sync_meta": False})

    await c.post("/webhooks/whatsapp", json=connect("wacid.bot", "571550000003"))
    await settle(0.6)
    call = await _call("wacid.bot")
    assert call.status == "connected" and call.handled_by == "voice_agent" and call.voice_agent_id
    assert ("wacid.bot", "accept", "v=0 bot-answer") in actions

    r = await c.post(f"/api/calls/{call.id}/hangup")
    assert r.json()["status"] == "ended"
    assert ("wacid.bot", "terminate", None) in actions and stopped == [True]
    # El webhook terminate que llega después es idempotente
    await c.post("/webhooks/whatsapp", json=terminate("wacid.bot", "571550000003"))
    await settle()
    assert (await _call("wacid.bot")).status == "ended"


async def test_voice_agent_unavailable_falls_back_to_humans(client, monkeypatch):
    c = client
    monkeypatch.setattr(agent_runtime, "runtime_available", lambda: False)
    monkeypatch.setattr(signaling, "RING_TIMEOUT_S", 0.2)
    # Canal con llamadas y agente de voz asignado (no depende del orden de las pruebas)
    bots = (await c.get("/api/bots")).json()
    va = await c.post("/api/voice/agents", json={"name": "Recepción respaldo", "ai_agent_id": bots[0]["id"],
                                                 "greeting": "Hola"})
    assert va.status_code == 200, va.text
    await c.put(f"/api/voice/channels/{await _channel_id()}", json={
        "calling_enabled": True, "voice_agent_id": va.json()["id"], "sync_meta": False, "calling_hours": None})
    await c.post("/webhooks/whatsapp", json=connect("wacid.fallback", "571550000004"))
    await settle(0.8)
    assert any(e == "call.incoming" and d["wa_call_id"] == "wacid.fallback" for e, d in events)
    call = await _call("wacid.fallback")
    assert call.status == "missed" and ("wacid.fallback", "reject", None) in actions


async def test_outside_hours_and_stats(client):
    c = client
    r = await c.put(f"/api/voice/channels/{await _channel_id()}", json={
        "calling_enabled": True, "voice_agent_id": None, "sync_meta": False,
        "calling_hours": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "00:01"}})
    assert r.status_code == 200
    bad = await c.put(f"/api/voice/channels/{await _channel_id()}", json={
        "calling_enabled": True, "sync_meta": False, "calling_hours": {"days": [0], "start": "18:00", "end": "08:00"}})
    assert bad.status_code == 422
    await c.post("/webhooks/whatsapp", json=connect("wacid.night", "571550000005"))
    await settle()
    call = await _call("wacid.night")
    assert call.status == "rejected" and "horario" in call.end_reason
    stats = (await c.get("/api/calls/stats")).json()
    # Solo lo que crea esta prueba (las demás del archivo pueden correr antes o después)
    assert stats["total"] >= 1 and stats["by_status"].get("rejected", 0) >= 1
    assert stats["minutes_month"]["metric"] == "voice_minutes_month"
    listing = (await c.get("/api/calls", params={"status": "rejected"})).json()
    assert listing["total"] >= 1
    await c.put(f"/api/voice/channels/{await _channel_id()}", json={"calling_enabled": False, "sync_meta": False})
