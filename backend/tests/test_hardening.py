"""Salida a producción: textos del bot agrupados (costos), nombre del webhook → llaves maestras, habeas data."""

import asyncio

from sqlalchemy import select

from app import bot_outbox
from app.auth import hash_password
from app.db import SessionLocal
from app.models import AIAgent, Agent, Attribution, Contact, ContactKey, Conversation, Message
from tests.conftest import WA, settle, text, unique_phone


async def _conv_for(c, phone: str) -> dict:
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_bot_texts_merge_when_cost_optimization(client):
    c = client
    phone = unique_phone()
    await c.post("/webhooks/whatsapp", json=text(phone, f"h.{phone}.1", "hola"))
    await settle()
    conv_id = (await _conv_for(c, phone))["id"]
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        before = sum(1 for to, _ in WA.sent if to == phone)
        # Con agrupación: dos textos en la ventana → un solo envío con ambos
        await bot_outbox.queue_text(s, conv, "Primera parte.", ai_agent_id=None, merge=True)
        await bot_outbox.queue_text(s, conv, "Segunda parte.", ai_agent_id=None, merge=True)
        assert bot_outbox.pending(conv_id) == ["Primera parte.", "Segunda parte."]
    await asyncio.sleep(bot_outbox.window_s() + 0.2)
    mine = [b for to, b in WA.sent if to == phone]
    assert len(mine) == before + 1 and mine[-1] == "Primera parte.\n\nSegunda parte."
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        # Sin agrupación: cada texto sale de inmediato
        await bot_outbox.queue_text(s, conv, "A", ai_agent_id=None, merge=False)
        await bot_outbox.queue_text(s, conv, "B", ai_agent_id=None, merge=False)
        await s.commit()
    assert [b for to, b in WA.sent if to == phone][-2:] == ["A", "B"]
    # flush explícito: envía ya (antes de transferir) y no vuelve a enviar al vencer la ventana
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        await bot_outbox.queue_text(s, conv, "Te paso con un asesor.", ai_agent_id=None, merge=True)
    await bot_outbox.flush(conv_id)
    count = len([b for to, b in WA.sent if to == phone])
    await asyncio.sleep(bot_outbox.window_s() + 0.2)
    assert len([b for to, b in WA.sent if to == phone]) == count
    async with SessionLocal() as s:
        stored = (await s.scalars(select(Message.text).where(Message.conversation_id == conv_id,
                                                             Message.sender_type == "bot"))).all()
    assert "Primera parte.\n\nSegunda parte." in stored


async def test_agent_reply_and_handoff_keep_order_with_merge(client):
    c = client
    async with SessionLocal() as s:
        for a in (await s.scalars(select(AIAgent))).all():
            a.cost_optimization = True
        await s.commit()
    phone = unique_phone()
    await c.post("/webhooks/whatsapp", json=text(phone, f"h.{phone}.2", "quiero un asesor"))
    await settle(0.8)
    conv = await _conv_for(c, phone)
    assert conv["status"] == "human"
    assert [b for to, b in WA.sent if to == phone][-1] == "Te paso con un asesor."
    async with SessionLocal() as s:
        msgs = (await s.execute(select(Message.sender_type, Message.text).where(
            Message.conversation_id == conv["id"]).order_by(Message.created_at, Message.id))).all()
    bot_idx = max(i for i, (st, _t) in enumerate(msgs) if st == "bot")
    assert all(st != "bot" for st, _t in msgs[bot_idx + 1:])  # nada del bot después de la transferencia


async def test_webhook_name_feeds_golden_names(client):
    c = client
    ch = (await c.get("/api/channels")).json()["channels"][0]
    r = await c.post("/api/inbound-webhooks", json={
        "name": f"Alta lead {unique_phone()}", "action": "upsert_contact", "channel_id": ch["id"],
        "params": [{"name": "telefono", "type": "phone", "required": True, "maps_to": "recipient.phone"},
                   {"name": "nombre", "type": "text", "required": False, "maps_to": "contact.name"}]})
    assert r.status_code == 200, r.text
    hook = r.json()
    await c.post(f"/api/inbound-webhooks/{hook['id']}/publish")
    phone = unique_phone("57310")
    call = await c.post(f"/hooks/{hook['slug']}", headers={"X-Hook-Token": hook["token"]},
                        json={"telefono": phone, "nombre": "María de los Ángeles Pérez Gómez"})
    assert call.status_code == 200, call.text
    cid = call.json()["contact_id"]
    async with SessionLocal() as s:
        keys = {k.key_type: k.value for k in (await s.scalars(select(ContactKey).where(
            ContactKey.contact_id == cid, ContactKey.status == "active"))).all()}
    assert keys.get("first_name") == "María de los Ángeles" and keys.get("last_name") == "Pérez Gómez"


async def test_privacy_export_and_erase(client):
    c = client
    phone = unique_phone()
    await c.post("/webhooks/whatsapp", json=text(phone, f"h.{phone}.3", "mi correo es ana@test.co y placa ABC123",
                                                 referral={"source_type": "ad", "source_id": "AD-PRIV",
                                                           "ctwa_clid": "CLID-PRIV"}))
    await settle()
    conv = await _conv_for(c, phone)
    cid = conv["contact"]["id"]
    await c.post(f"/api/contacts/{cid}/keys", json={"key_type": "document", "subtype": "CC", "value": "1020345678"})

    exp = await c.get(f"/api/contacts/{cid}/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    data = exp.json()
    assert data["contact"]["wa_id"] == phone
    assert any(k["key_type"] == "document" for k in data["contact_keys"])
    assert any(m["text"] and "placa" in m["text"] for cv in data["conversations"] for m in cv["messages"])
    assert data["attributions"][0]["ctwa_clid"] == "CLID-PRIV"
    assert all("token_hash" not in row for rows in data.values() if isinstance(rows, list) for row in rows)

    # Un asesor sin el permiso no puede
    async with SessionLocal() as s:
        if not await s.scalar(select(Agent.id).where(Agent.email == "privacy-agent@test.com")):
            s.add(Agent(organization_id=1, email="privacy-agent@test.com", name="Asesor Privacidad", role="agent",
                        password_hash=hash_password("secret-agent-123")))
            await s.commit()
    login = await c.post("/api/auth/login", json={"email": "privacy-agent@test.com", "password": "secret-agent-123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await c.get(f"/api/contacts/{cid}/export", headers=headers)).status_code == 403

    assert (await c.request("DELETE", f"/api/contacts/{cid}/erase", json={"confirm": "si"})).status_code == 422
    r = await c.request("DELETE", f"/api/contacts/{cid}/erase", json={"confirm": "ELIMINAR"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        contact = await s.get(Contact, cid)
        assert contact.name == "[eliminado]" and contact.wa_id is None and contact.email is None
        assert contact.conversations_count >= 1 and contact.messages_in >= 1  # agregados intactos
        texts = (await s.scalars(select(Message.text).join(Conversation, Conversation.id == Message.conversation_id)
                                 .where(Conversation.contact_id == cid))).all()
        assert texts and all(t is None for t in texts)
        assert not (await s.scalars(select(ContactKey).where(ContactKey.contact_id == cid))).all()
        attr = (await s.scalars(select(Attribution).where(Attribution.contact_id == cid))).first()
        assert attr.ctwa_clid is None and attr.channel == "meta_ctwa"  # canal se conserva para reportes
    assert (await c.get(f"/api/contacts/{cid}/export")).json()["contact"]["name"] == "[eliminado]"


async def test_saas_company_never_inherits_server_waba(client):
    """Regresión: un canal creado por otra empresa sin WABA no hereda WA_WABA_ID del servidor (los eventos de la
    cuenta de la plataforma — plantillas, calidad, alertas — llegaban a la empresa equivocada)."""
    import httpx

    from app.main import app
    from app.models import Channel, Organization

    org = 9356
    async with SessionLocal() as s:
        if not await s.get(Organization, org):
            s.add(Organization(id=org, name="Otra SaaS", slug="otra-saas-9356", timezone="America/Bogota"))
            await s.flush()
            s.add(Agent(organization_id=org, email="admin9356@test.com", name="Admin 9356", role="admin",
                        password_hash=hash_password("secret-9356-abc")))
            await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c2:
        tok = (await c2.post("/api/auth/login", json={"email": "admin9356@test.com",
                                                       "password": "secret-9356-abc"})).json()["access_token"]
        c2.headers["Authorization"] = f"Bearer {tok}"
        r = await c2.post("/api/channels", json={"name": "WA otra", "phone_number_id": f"PN-{unique_phone()}"})
        assert r.status_code == 200, r.text
        assert (await c2.get("/api/channels")).json()["waba_id"] is None
    async with SessionLocal() as s:
        ch = await s.get(Channel, r.json()["id"])
        assert ch.waba_id is None
    # La empresa de la instalación sí conserva la WABA del servidor
    assert (await client.get("/api/channels")).json()["waba_id"] == "WABA"
