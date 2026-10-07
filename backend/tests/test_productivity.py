"""Productividad del asesor (§18.3): notificaciones, recordatorios, menciones, respuestas rápidas v2, iniciar
conversación, plantilla masiva desde la selección, disparador «Cuando se tipifica» y línea de tiempo."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app import storage
from app.auth import hash_password
from app.db import SessionLocal
from app.models import (
    Agent,
    Contact,
    Conversation,
    FlowRun,
    FollowUp,
    Group,
    Notification,
    Organization,
    QuickReply,
    Resource,
    utcnow,
)
from app.notifications import notify, remind_due
from app.realtime import hub
from app.whatsapp import WhatsAppClient
from tests.conftest import WA, settle, text

media_sent: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def fake_media(monkeypatch):
    async def upload_media(self, data, mime, filename):
        return "MEDIA1"

    async def send_media(self, to, kind, media_id, caption=None, filename=None):
        media_sent.append((to, filename or kind))
        return "wamid.media"

    monkeypatch.setattr(WhatsAppClient, "upload_media", upload_media)
    monkeypatch.setattr(WhatsAppClient, "send_media", send_media)


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, payload: str):
        self.sent.append(json.loads(payload))


async def _admin() -> Agent:
    async with SessionLocal() as s:
        return await s.scalar(select(Agent).where(Agent.email == "admin@test.com"))


async def _agent(email: str, name: str) -> Agent:
    async with SessionLocal() as s:
        a = await s.scalar(select(Agent).where(Agent.email == email))
        if not a:
            a = Agent(organization_id=1, email=email, name=name, role="agent", password_hash=hash_password("secret12"))
            s.add(a)
            await s.commit()
        return a


async def _conv(c, phone: str, body: str = "hola") -> dict:
    await c.post("/webhooks/whatsapp", json=text(phone, f"pr.{phone}.{body[:6]}", body))
    await settle()
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_notifications_prefs_realtime_and_read(client):
    c = client
    admin = await _admin()
    sock = FakeSocket()
    other = FakeSocket()
    hub.sockets[sock] = (1, admin.id)
    hub.sockets[other] = (1, 999999)  # otro asesor de la misma empresa: no recibe
    try:
        async with SessionLocal() as s:
            await notify(s, admin.id, "system", "Hola", "Prueba", "/", {"k": 1})
        assert sock.sent and sock.sent[-1]["event"] == "notification.new"
        assert sock.sent[-1]["data"]["title"] == "Hola"
        assert not other.sent
    finally:
        hub.sockets.pop(sock, None)
        hub.sockets.pop(other, None)

    # Preferencias: menciones apagadas → no se guarda
    r = await c.put("/api/me/notification-prefs", json={"types": {"mention": False}, "sound": False})
    assert r.status_code == 200 and r.json()["types"]["mention"] is False and r.json()["sound"] is False
    assert (await c.put("/api/me/notification-prefs", json={"types": {"nope": True}})).status_code == 422
    async with SessionLocal() as s:
        await notify(s, admin.id, "mention", "No debería guardarse")
    items = (await c.get("/api/notifications")).json()["items"]
    assert not any(i["title"] == "No debería guardarse" for i in items)
    await c.put("/api/me/notification-prefs", json={"types": {"mention": True}, "sound": True})

    unread = (await c.get("/api/notifications/unread-count")).json()["unread"]
    assert unread >= 1
    first = (await c.get("/api/notifications", params={"unread": True, "limit": 1})).json()
    assert len(first["items"]) == 1
    r = await c.post("/api/notifications/read", json={"ids": [first["items"][0]["id"]]})
    assert r.json()["unread"] == unread - 1
    assert (await c.post("/api/notifications/read", json={"all": True})).json()["unread"] == 0
    assert (await c.post("/api/notifications/read", json={})).status_code == 422


async def test_followup_reminder_fires_once(client):
    admin = await _admin()
    async with SessionLocal() as s:
        contact = Contact(organization_id=1, wa_id="573918880001", name="Recordar Pérez")
        s.add(contact)
        await s.flush()
        s.add(FollowUp(organization_id=1, contact_id=contact.id, agent_id=admin.id, kind="callback",
                       due_at=utcnow() - timedelta(minutes=5), note="Llamar por la cotización"))
        await s.commit()
    assert await remind_due() >= 1
    assert await remind_due() == 0  # una sola vez
    async with SessionLocal() as s:
        n = (await s.scalars(select(Notification).where(Notification.agent_id == admin.id,
                                                         Notification.type == "callback_due")
                             .order_by(Notification.id.desc()))).first()
    assert n and n.title == "Llamar a Recordar Pérez" and n.body == "Llamar por la cotización"


async def test_mentions_in_internal_notes(client):
    c = client
    luis = await _agent("zacarias.mencion@test.com", "Zacarías Mención")
    conv = await _conv(c, "573918880002")
    r = await c.post(f"/api/conversations/{conv['id']}/notes", json={"text": "@Zacarías revisa este caso por favor"})
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["mentioned"]] == [luis.id]
    assert r.json()["message"]["sender_type"] == "system" and "Nota de" in r.json()["message"]["text"]
    async with SessionLocal() as s:
        n = await s.scalar(select(Notification).where(Notification.agent_id == luis.id, Notification.type == "mention"))
    assert n and n.link == f"/conversaciones?id={conv['id']}"
    assert (await c.post(f"/api/conversations/{conv['id']}/notes", json={"text": "  "})).status_code == 422
    assert not WA.sent or WA.sent[-1][1] != "@Zacarías revisa este caso por favor"  # nunca llega al cliente


async def test_quick_replies_v2_groups_variables_attachments(client):
    c = client
    async with SessionLocal() as s:
        g = Group(organization_id=1, name="Taller QR")
        s.add(g)
        await s.flush()
        path = await storage.upload(storage.new_path(storage.RESOURCES_BUCKET, 1, "library", "application/pdf"),
                                    b"%PDF-1.4 test", "application/pdf")
        res = Resource(organization_id=1, name="cotizacion.pdf", storage_path=path, mime="application/pdf",
                       size_bytes=13)
        s.add(res)
        await s.commit()
        gid, rid = g.id, res.id
    r = await c.post("/api/quick-replies", json={"shortcut": "saludo-qr", "text": "Hola {{client_first_name}}, soy {{agent_name}} de {{company_name}}",
                                                  "title": "Saludo", "category": "General"})
    assert r.status_code == 200, r.text
    r = await c.post("/api/quick-replies", json={"shortcut": "taller-qr", "text": "Te escribe {{group_name}}",
                                                  "title": "Taller", "category": "Posventa", "group_ids": [gid],
                                                  "resource_ids": [rid]})
    assert r.status_code == 200 and r.json()["attachments"][0]["name"] == "cotizacion.pdf"
    assert (await c.post("/api/quick-replies", json={"shortcut": "x-qr", "text": "x", "group_ids": [999999]})).status_code == 404

    conv = await _conv(c, "573918880003", "hola quiero info")
    avail = (await c.get("/api/quick-replies", params={"conversation_id": conv["id"]})).json()
    keys = {q["shortcut"]: q for q in avail}
    assert "saludo-qr" in keys and "taller-qr" not in keys  # sin grupo: solo las de todos los grupos
    assert keys["saludo-qr"]["rendered"].startswith("Hola Ana, soy ")

    async with SessionLocal() as s:
        await s.execute(update(Conversation).where(Conversation.id == conv["id"]).values(group_id=gid))
        await s.commit()
    avail = {q["shortcut"]: q for q in (await c.get("/api/quick-replies", params={"conversation_id": conv["id"]})).json()}
    assert avail["taller-qr"]["rendered"] == "Te escribe Taller QR"
    assert [q["shortcut"] for q in (await c.get("/api/quick-replies", params={"q": "posventa"})).json()] == ["taller-qr"]

    before = len(media_sent)
    r = await c.post(f"/api/conversations/{conv['id']}/quick-replies/{avail['taller-qr']['id']}/send", json={})
    assert r.status_code == 200, r.text
    assert WA.sent[-1] == ("573918880003", "Te escribe Taller QR")
    assert len(media_sent) == before + 1 and media_sent[-1][1] == "cotizacion.pdf"
    async with SessionLocal() as s:
        assert (await s.get(QuickReply, avail["taller-qr"]["id"])).usage_count == 1


async def test_start_conversation_window_and_bsuid(client):
    c = client
    admin = await _admin()
    # Cliente con conversación reciente: texto libre dentro de la ventana, queda asignado a mí
    conv = await _conv(c, "573918880004")
    r = await c.post("/api/contacts/start-conversation", json={"contact_id": conv["contact"]["id"],
                                                               "text": "Hola, te escribo por tu solicitud"})
    assert r.status_code == 200, r.text
    assert r.json()["window_open"] and r.json()["conversation"]["assigned_agent"]["id"] == admin.id
    assert WA.sent[-1] == ("573918880004", "Hola, te escribo por tu solicitud")

    # Cliente sin conversación: texto libre no; plantilla sí; bot on
    async with SessionLocal() as s:
        cold = Contact(organization_id=1, wa_id="573918880005", name="Frío Gómez")
        bsuid_only = Contact(organization_id=1, wa_bsuid="CO.99887766", name="Solo Usuario")
        s.add_all([cold, bsuid_only])
        await s.commit()
        cold_id, bsuid_id = cold.id, bsuid_only.id
    r = await c.post("/api/contacts/start-conversation", json={"contact_id": cold_id, "text": "Hola"})
    assert r.status_code == 409
    r = await c.post("/api/contacts/start-conversation", json={
        "contact_id": cold_id, "template_name": "recordatorio", "language": "es", "params": ["mañana"], "bot": "on"})
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["status"] == "bot" and WA.templates_sent[-1][0] == "573918880005"
    # mode=new abre otra conversación
    first_id = r.json()["conversation"]["id"]
    r = await c.post("/api/contacts/start-conversation", json={
        "contact_id": cold_id, "template_name": "recordatorio", "language": "es", "params": ["hoy"], "mode": "new"})
    assert r.json()["conversation"]["id"] != first_id
    # Solo BSUID (sin teléfono): la plantilla de utilidad va por recipient
    r = await c.post("/api/contacts/start-conversation", json={
        "contact_id": bsuid_id, "template_name": "recordatorio", "language": "es", "params": ["hoy"]})
    assert r.status_code == 200, r.text
    assert WA.templates_sent[-1][0] in ("CO.99887766", {"recipient": "CO.99887766"}) or "CO.99887766" in str(WA.templates_sent[-1][0])
    assert (await c.post("/api/contacts/start-conversation", json={"contact_id": 999999, "text": "x"})).status_code == 404


async def test_campaign_from_selection_skips_opt_out(client):
    c = client
    async with SessionLocal() as s:
        a = Contact(organization_id=1, wa_id="573918880006", name="Sel Uno")
        b = Contact(organization_id=1, wa_id="573918880007", name="Sel Dos", marketing_opt_out=True,
                    opt_out_at=utcnow())
        s.add_all([a, b])
        await s.commit()
        ids = [a.id, b.id]
    admin = await _admin()
    r = await c.post("/api/campaigns/from-selection", json={
        "contact_ids": ids, "template_name": "promo", "template_language": "es", "params": ["{{nombre}}", "un bono"],
        "options": {"bot": "off", "assign_agent_id": admin.id, "tags": ["seleccion"], "conversation": "continue",
                    "ignored": 1}})
    assert r.status_code == 200, r.text
    camp = r.json()
    assert camp["recipients"] == 2 and camp["source"] == "client_list" and "ignored" not in camp["options"]
    await settle(0.8)
    detail = (await c.get(f"/api/campaigns/{camp['id']}")).json()
    by_phone = {x["wa_id"]: x for x in detail["recipients"]}
    assert by_phone["573918880006"]["status"] == "sent"
    assert by_phone["573918880007"]["status"] == "skipped"  # opt-out de marketing
    conv = (await c.get("/api/conversations", params={"q": "573918880006"})).json()[0]
    assert conv["status"] == "human" and conv["assigned_agent"]["id"] == admin.id and "seleccion" in conv["tags"]
    assert (await c.post("/api/campaigns/from-selection", json={
        "template_name": "promo", "template_language": "es"})).status_code == 422
    r = await c.post("/api/campaigns/from-selection", json={
        "filter": {"q": "Sel Uno", "created_from": "2020-01-01", "has_products": "false"},
        "template_name": "recordatorio", "template_language": "es", "params": ["x"],
        "start": False})
    assert r.status_code == 200 and r.json()["recipients"] == 1 and r.json()["status"] == "draft"


async def test_typified_trigger_starts_flow_and_timeline(client):
    c = client
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Pendiente respuesta cliente"]})
    definition = {"schema_version": 1, "variables": [], "scripts": [{
        "id": "s1", "trigger": {"type": "typified", "config": {"typifications": ["Pendiente respuesta cliente"]}},
        "blocks": [{"id": "b1", "type": "send_text", "inputs": {"text": "Seguimos pendientes, {{contact.first_name}}"}}]}]}
    r = await c.post("/api/flows", json={"name": "Seguimiento tipificación", "trigger_type": "typified",
                                         "trigger_config": {}, "editor_mode": "advanced"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    assert (await c.post(f"/api/flows/{fid}/versions", json={"definition": definition})).status_code == 200
    assert (await c.post(f"/api/flows/{fid}/publish", json={})).status_code == 200

    conv = await _conv(c, "573918880008")
    await c.post(f"/api/conversations/{conv['id']}/assign")
    r = await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Venta"})
    assert r.status_code == 200, r.text
    await settle(0.6)
    assert WA.sent[-1][1] != "Seguimos pendientes, Ana"  # otra tipificación: no dispara

    conv2 = await _conv(c, "573918880009")
    await c.post(f"/api/conversations/{conv2['id']}/assign")
    await c.post(f"/api/conversations/{conv2['id']}/close", json={"typification": "Pendiente respuesta cliente"})
    await settle(0.6)
    assert WA.sent[-1] == ("573918880009", "Seguimos pendientes, Ana")
    async with SessionLocal() as s:
        run = await s.scalar(select(FlowRun).where(FlowRun.conversation_id == conv2["id"], FlowRun.flow_id == fid))
    assert run and run.trigger_type == "typified"
    await c.post(f"/api/flows/{fid}/pause")

    tl = (await c.get(f"/api/conversations/{conv2['id']}/timeline")).json()["items"]
    types = [i["type"] for i in tl]
    assert types[0] == "created" and "assigned" in types and "closed" in types
    typ = next(i for i in tl if i["type"] == "typified")
    assert typ["detail"] == "Pendiente respuesta cliente" and typ["label"] == "Tipificada"
    assigned = next(i for i in tl if i["type"] == "assigned")
    assert assigned["detail"] and assigned["actor"]


async def test_org_isolation(client):
    c = client
    async with SessionLocal() as s:
        other = await s.get(Organization, 9481) or Organization(id=9481, name="Otra prod", slug="otra-prod")
        s.add(other)
        await s.flush()
        stranger = Agent(organization_id=9481, email="extrano@prod.test", name="Extraño", role="admin",
                         password_hash=hash_password("secret12"))
        s.add(stranger)
        await s.flush()
        s.add(Notification(organization_id=9481, agent_id=stranger.id, type="system", title="Secreto"))
        qr = QuickReply(organization_id=9481, shortcut="secreto-qr", text="no")
        s.add(qr)
        contact = Contact(organization_id=9481, wa_id="573918880099", name="Ajeno")
        s.add(contact)
        await s.commit()
        qr_id, contact_id = qr.id, contact.id
    items = (await c.get("/api/notifications", params={"limit": 100})).json()["items"]
    assert not any(i["title"] == "Secreto" for i in items)
    assert not any(q["shortcut"] == "secreto-qr" for q in (await c.get("/api/quick-replies")).json())
    conv = await _conv(c, "573918880010")
    assert (await c.post(f"/api/conversations/{conv['id']}/quick-replies/{qr_id}/send", json={})).status_code == 404
    assert (await c.post("/api/contacts/start-conversation", json={"contact_id": contact_id, "text": "x"})).status_code == 404
    assert (await c.get("/api/conversations/999999/timeline")).status_code == 404
