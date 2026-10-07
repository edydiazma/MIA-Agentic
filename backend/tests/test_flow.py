"""Flujo básico sobre Postgres: bot multimodal, handoff, asesor, cierre con tipificación y hechos generados."""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ConversationEvent, InboundEvent
from tests.conftest import PNG, WA, FakeChat, inbound, settle, text

PHONE = "573001112233"


async def _conv(c, phone=PHONE):
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_full_flow(client):
    c = client
    r = await c.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert r.text == "42"

    # 1. Texto -> el bot responde
    await c.post("/webhooks/whatsapp", json=text(PHONE, "wamid.1", "hola"))
    await settle()
    assert WA.sent[-1] == (PHONE, "¡Hola! ¿En qué te ayudo?")
    n = len(WA.sent)

    # Reintento de Meta: no duplica (message_wa_ids)
    await c.post("/webhooks/whatsapp", json=text(PHONE, "wamid.1", "hola"))
    await settle()
    assert len(WA.sent) == n

    # 2. Imagen -> se guarda en el almacenamiento y llega al modelo como imagen
    await c.post("/webhooks/whatsapp", json=inbound(PHONE, "wamid.2", {"type": "image", "image": {"id": "m1"}}))
    await settle()
    assert WA.sent[-1][1] == "Veo tu imagen 👀"

    # 3. Pide asesor -> handoff
    await c.post("/webhooks/whatsapp", json=text(PHONE, "wamid.3", "quiero un asesor"))
    await settle()
    conv = await _conv(c)
    assert conv["status"] == "human"
    assert conv["handoff_reason"] == "pidió asesor" and conv["handoff_at"]
    conv_id = conv["id"]

    # 4. Con la conversación en humano el bot ya no responde
    n = len(WA.sent)
    await c.post("/webhooks/whatsapp", json=text(PHONE, "wamid.4", "¿hola?"))
    await settle()
    assert len(WA.sent) == n

    # 5. El asesor responde desde la bandeja: el trigger registra la primera respuesta
    r = await c.post(f"/api/conversations/{conv_id}/messages", json={"text": "Hola Ana, soy Luis"})
    assert r.status_code == 200, r.text
    assert WA.sent[-1][1] == "Hola Ana, soy Luis"
    conv = (await c.get(f"/api/conversations/{conv_id}")).json()
    assert conv["first_response_at"] and conv["message_count"] == 8

    msgs = (await c.get(f"/api/conversations/{conv_id}/messages")).json()
    assert [m["sender_type"] for m in msgs if m["sender_type"] != "system"] == [
        "contact", "bot", "contact", "bot", "contact", "bot", "contact", "agent"]
    img = next(m for m in msgs if m["type"] == "image")
    r = await c.get(f"/api/media/{img['id']}", params={"token": c.token}, headers={"Authorization": ""})
    assert r.status_code == 200 and r.content == PNG

    # 6. Devolver al bot y cerrar (exige tipificación)
    assert (await c.post(f"/api/conversations/{conv_id}/release")).json()["status"] == "bot"
    assert (await c.post(f"/api/conversations/{conv_id}/close")).status_code == 422
    r = await c.post(f"/api/conversations/{conv_id}/close", json={"typification": "Venta"})
    assert r.json()["status"] == "closed" and r.json()["typification"] == "Venta" and r.json()["closed_at"]

    # Hechos generados por la base (triggers), con actor
    async with SessionLocal() as s:
        events = (await s.execute(select(ConversationEvent.event_type, ConversationEvent.actor_type)
                                  .where(ConversationEvent.conversation_id == conv_id)
                                  .order_by(ConversationEvent.occurred_at, ConversationEvent.id))).all()
        raw = (await s.scalars(select(InboundEvent).order_by(InboundEvent.id.desc()).limit(1))).first()
    types = [e for e, _ in events]
    for expected in ("created", "handoff", "first_agent_response", "released", "closed"):
        assert expected in types, (expected, types)
    assert ("handoff", "bot") in events
    assert raw is not None and raw.processed_at is not None and raw.error is None

    # 7. Si todas las conexiones del Cortex fallan, se transfiere a un humano
    await c.post("/webhooks/whatsapp", json=text("573009990000", "wamid.5", "falla total"))
    await settle()
    conv = await _conv(c, "573009990000")
    assert conv["status"] == "human" and "IA no respondió" in conv["handoff_reason"]
    assert FakeChat.requests  # el agente sí llamó al Cortex
