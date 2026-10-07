"""Módulos de la plataforma: grupos y asignación, automatizaciones, bloqueos, Click to WA,
estados con facturación y opt-out, alertas de Meta, plantillas, campañas, citas,
seguimiento, base de conocimiento y reportes."""

from datetime import UTC, datetime, timedelta

from app.realtime import hub
from tests.conftest import WA, FakeChat, settle, text


async def _conv(c, phone):
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_groups_and_auto_assign(client, monkeypatch):
    c = client
    g = (await c.post("/api/groups", json={"name": "Ventas"})).json()
    a = (await c.post("/api/agents", json={
        "email": "luis@test.com", "name": "Luis", "password": "12345678", "group_ids": [g["id"]]})).json()
    assert a["group_ids"] == [g["id"]]
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: {a["id"]})

    await c.post("/webhooks/whatsapp", json=text("571110000001", "g.1", "quiero un asesor de ventas"))
    await settle()
    conv = await _conv(c, "571110000001")
    assert conv["status"] == "human"
    assert conv["group"]["name"] == "Ventas"
    assert conv["assigned_agent"]["id"] == a["id"]

    # Actuar como agente (admin)
    mine = (await c.get("/api/conversations", params={"agent_id": a["id"]})).json()
    assert [x["id"] for x in mine] == [conv["id"]]

    # Transferencia a otro grupo sin asesor
    g2 = (await c.post("/api/groups", json={"name": "Posventa"})).json()
    r = await c.post(f"/api/conversations/{conv['id']}/transfer", json={"group_id": g2["id"], "note": "garantía"})
    assert r.json()["group"]["name"] == "Posventa" and r.json()["assigned_agent"] is None
    assert len((await c.get("/api/conversations", params={"status": "unassigned"})).json()) >= 1


async def test_automations(client):
    c = client
    await c.post("/api/automations", json={"name": "Bienvenida", "type": "welcome",
                                           "config": {"message": "¡Bienvenido a la tienda!"}})
    await c.post("/api/automations", json={"name": "Horario", "type": "keyword_reply",
                                           "config": {"keywords": ["horario"], "reply": "Atendemos de 8 a 6."}})
    await c.post("/api/automations", json={"name": "Reclamos", "type": "keyword_handoff",
                                           "config": {"keywords": ["reclamo"], "message": "Te paso con servicio."}})
    assert (await c.post("/api/automations", json={"name": "x", "type": "nope"})).status_code == 422

    phone = "571110000002"
    n_req = len(FakeChat.requests)
    await c.post("/webhooks/whatsapp", json=text(phone, "a.1", "¿Cuál es el HORARIO?"))
    await settle()
    assert WA.sent[-2:] == [(phone, "¡Bienvenido a la tienda!"), (phone, "Atendemos de 8 a 6.")]
    assert len(FakeChat.requests) == n_req  # la regla respondió: no se llamó a la IA

    await c.post("/webhooks/whatsapp", json=text(phone, "a.2", "tengo un reclamo"))
    await settle()
    conv = await _conv(c, phone)
    assert conv["status"] == "human" and conv["handoff_reason"] == "Palabra clave: reclamo"

    for a in (await c.get("/api/automations")).json():
        await c.delete(f"/api/automations/{a['id']}")


async def test_blocked_and_ctwa(client):
    c = client
    phone = "571110000003"
    await c.post("/webhooks/whatsapp", json=text(phone, "b.1", "hola", referral={
        "source_type": "ad", "source_id": "AD123", "headline": "Chevrolet Onix 0 km", "ctwa_clid": "clid"}))
    await settle()
    conv = await _conv(c, phone)
    assert conv["ad_source_type"] == "ad" and conv["ad_headline"] == "Chevrolet Onix 0 km"
    assert "«Chevrolet Onix 0 km»" in FakeChat.requests[-1].context  # bloque «Origen del cliente» (§17)
    ctwa = (await c.get("/api/reports/ctwa")).json()
    assert any(a["ad_id"] == "AD123" for a in ctwa["ads"])  # otras pruebas también crean anuncios en la org 1

    contact_id = conv["contact"]["id"]
    await c.post(f"/api/contacts/{contact_id}/block", json={"reason": "spam"})
    assert (await _conv(c, phone))["status"] == "closed"
    n = len(WA.sent)
    await c.post("/webhooks/whatsapp", json=text(phone, "b.2", "hola otra vez"))
    await settle()
    assert len(WA.sent) == n
    blocked = (await c.get("/api/contacts", params={"blocked": True})).json()
    assert blocked["items"][0]["wa_id"] == phone
    await c.post(f"/api/contacts/{contact_id}/unblock")
    still = (await c.get("/api/contacts", params={"blocked": True})).json()["items"]
    assert all(x["wa_id"] != phone for x in still)  # otras pruebas pueden dejar contactos bloqueados en la org 1


async def test_statuses_billing_optout_and_meta_alerts(client):
    c = client
    phone = "571110000004"
    await c.post("/webhooks/whatsapp", json=text(phone, "s.1", "hola"))
    await settle()
    msgs = (await c.get(f"/api/conversations/{(await _conv(c, phone))['id']}/messages")).json()
    out_id = next(m for m in msgs if m["direction"] == "out")
    wamid = None
    # buscamos el wamid real a través de la BD
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Message

    async with SessionLocal() as s:
        wamid = await s.scalar(select(Message.wa_message_id).where(Message.id == out_id["id"]))

    def status(st, **extra):
        return {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID"},
            "statuses": [{"id": wamid, "status": st, "recipient_id": phone, **extra}]}}]}]}

    await c.post("/webhooks/whatsapp", json=status("delivered", pricing={
        "billable": True, "pricing_model": "PMP", "category": "service"}))
    await c.post("/webhooks/whatsapp", json=status("read"))
    await c.post("/webhooks/whatsapp", json=status("delivered"))  # llega tarde: no retrocede
    await settle(0.1)
    msgs = (await c.get(f"/api/conversations/{(await _conv(c, phone))['id']}/messages")).json()
    assert next(m for m in msgs if m["id"] == out_id["id"])["status"] == "read"
    billing = (await c.get("/api/reports/billing")).json()
    assert billing["categories"]["service"]["billable"] >= 1

    await c.post("/webhooks/whatsapp", json=status("failed", errors=[{"code": 131050, "title": "opt-out"}]))
    await c.post("/webhooks/whatsapp", json={"entry": [{"id": "WABA", "changes": [{
        "field": "message_template_status_update",
        "value": {"event": "PAUSED", "message_template_name": "promo", "reason": "Calidad baja"}}]}]})
    await settle(0.1)
    # El Centro de Control cuenta la copia sincronizada de plantillas (wa_templates), no consulta a Meta en vivo
    assert len((await c.get("/api/templates")).json()) == 2
    cc = (await c.get("/api/control-center")).json()
    titles = [a["title"] for a in cc["alerts"]]
    assert "Un contacto pidió dejar de recibir marketing" in titles
    assert "Plantilla «promo» paused" in titles
    assert cc["meta_summary"]["opt_out_7d"] >= 1
    assert cc["meta_summary"]["templates_total"] == 2

    # Plantilla individual de marketing a un contacto con opt-out: bloqueada
    conv = await _conv(c, phone)
    r = await c.post(f"/api/conversations/{conv['id']}/template",
                     json={"name": "promo", "language": "es", "values": ["Ana", "un bono"]})
    assert r.status_code == 409
    r = await c.post(f"/api/conversations/{conv['id']}/template",
                     json={"name": "recordatorio", "language": "es", "values": ["5 de octubre"]})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "*Recordatorio*\nTu cita es el 5 de octubre"
    assert WA.templates_sent[-1][2] == [{"type": "body", "parameters": [
        {"type": "text", "text": "5 de octubre", "parameter_name": "fecha"}]}]


async def test_campaign(client):
    c = client
    for p in ("571220000001", "571220000002"):
        await c.post("/api/contacts", json={"wa_id": f"+{p}", "name": "Carlos Pérez", "tags": ["VIP"]})
    r = await c.post("/api/campaigns", json={
        "name": "Bonos octubre", "template_name": "promo", "template_language": "es",
        "params": ["{{nombre}}", "un bono"], "tag": "vip"})
    camp = r.json()
    assert camp["total"] == 2 and camp["status"] == "draft"
    await c.post(f"/api/campaigns/{camp['id']}/start")
    await settle(0.5)
    detail = (await c.get(f"/api/campaigns/{camp['id']}")).json()
    assert detail["status"] == "done" and detail["sent"] == 2
    assert WA.templates_sent[-1][2][0]["parameters"][0]["text"] == "Carlos"
    cc = (await c.get("/api/control-center")).json()
    assert cc["campaigns"][0]["name"] == "Bonos octubre"


async def test_appointments_by_bot_and_followups(client):
    c = client
    await c.put("/api/settings/appointments", json={"enabled": True, "days": [0, 1, 2, 3, 4, 5, 6],
                                                    "start": "08:00", "end": "18:00", "title": "Test drive"})
    day = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    phone = "571110000005"
    await c.post("/webhooks/whatsapp", json=text(phone, "c.1", f"cita {day}"))
    await settle()
    assert WA.sent[-1][1].startswith(f"Cita agendada para el {day} a las 08:00")
    appts = (await c.get("/api/appointments", params={"start": day, "end": day})).json()
    assert appts[0]["created_by"] == "bot" and appts[0]["title"] == "Test drive"
    slots = (await c.get("/api/appointments/availability", params={"day": day})).json()
    assert not slots[0].startswith(f"{day}T08:00")  # ese horario ya se ocupó

    conv = await _conv(c, phone)
    due = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    f = (await c.post("/api/followups", json={
        "contact_id": conv["contact"]["id"], "conversation_id": conv["id"], "due_at": due,
        "note": "Llamar para confirmar"})).json()
    assert f["overdue"] is True
    assert (await c.get("/api/control-center")).json()["me"]["followups_due"] >= 1
    await c.put(f"/api/followups/{f['id']}", json={"done": True})
    assert (await c.get("/api/followups")).json() == []


async def test_knowledge_settings_and_reports(client):
    c = client
    bot = (await c.get("/api/bots")).json()[0]
    await c.post("/api/knowledge", json={"bot_id": bot["id"], "title": "Precios", "content": "El Onix cuesta 70M"})
    up = await c.post("/api/knowledge/upload", data={"bot_id": str(bot["id"])},
                      files={"file": ("faq.md", b"# FAQ\nHorario 8-6", "text/markdown")})
    assert up.json()["title"] == "faq"
    await c.post("/webhooks/whatsapp", json=text("571110000006", "k.1", "hola"))
    await settle()
    system = FakeChat.requests[-1].system
    assert "## Base de conocimiento" in system and "El Onix cuesta 70M" in system and "Horario 8-6" in system

    assert (await c.put("/api/settings/company", json={"timezone": "Marte/Base"})).status_code == 422
    assert (await c.put("/api/settings/company", json={"name": "Automercol"})).json()["name"] == "Automercol"
    await c.post("/api/quick-replies", json={"shortcut": "/saludo", "text": "¡Hola! Soy tu asesor."})
    assert (await c.get("/api/quick-replies")).json()[0]["shortcut"] == "saludo"

    for path in ("/api/reports/realtime", "/api/reports/general", "/api/reports/stages", "/api/reports/inbound",
                 "/api/reports/outbound", "/api/reports/agents", "/api/reports/billing", "/api/integrations",
                 "/api/channels", "/api/alerts", "/api/contacts/tags", "/api/templates"):
        r = await c.get(path)
        assert r.status_code == 200, (path, r.text)
    general = (await c.get("/api/reports/general")).json()
    assert general["totals"]["handoffs"] >= 2 and general["totals"]["inbound_messages"] > 5
    csv_r = await c.get("/api/reports/conversations.csv")
    assert csv_r.text.lstrip("﻿").startswith("id,telefono,nombre")


async def test_outbound_webhook_signature(client, monkeypatch):
    c = client
    import httpx

    from app import webhooks_out

    received = []

    class FakeHTTP:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            received.append((url, content, headers))
            return httpx.Response(200)

    monkeypatch.setattr(webhooks_out.httpx, "AsyncClient", FakeHTTP)
    assert (await c.post("/api/outbound-webhooks", json={"name": "x", "url": "http://inseguro"})).status_code == 422
    w = (await c.post("/api/outbound-webhooks", json={
        "name": "CRM", "url": "https://crm.example.com/hook", "events": ["conversation.handoff"]})).json()
    await c.post("/webhooks/whatsapp", json=text("571110000007", "w.1", "quiero un asesor"))
    await settle(0.5)
    hooks = [r for r in received if b"conversation.handoff" in r[1]]
    assert hooks and hooks[0][2]["X-Signature-256"] == webhooks_out.sign(w["secret"], hooks[0][1])
    assert not any(b'"message.new"' in r[1] for r in received)  # solo eventos suscritos
