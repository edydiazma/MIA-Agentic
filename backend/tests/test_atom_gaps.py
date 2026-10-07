"""Brechas restantes vs. Atom (§19.1): canal de correo (Postmark, SendGrid, Mailgun, IMAP), botón flotante de
WhatsApp, opciones avanzadas del chat web y opciones de enrutamiento de /v1/messages."""

import base64
import hashlib
import hmac
import json
import time
from email.message import EmailMessage

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.auth import hash_password
from app.channels import email as mail
from app.db import SessionLocal
from app.models import Agent, Channel, Conversation, Group, Message, Organization, WaWidget
from app.main import app
from tests.conftest import settle, text

sent: list[tuple[dict, EmailMessage]] = []


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    sent.clear()

    def smtp_send(cfg, password, message):
        sent.append((cfg | {"password": password}, message))

    monkeypatch.setattr(mail, "smtp_send", smtp_send)

    async def no_limit(*a, **k):
        return None

    import app.routers.email_inbound as router_mod

    monkeypatch.setattr(router_mod, "enforce_limit", no_limit)


@pytest.fixture(autouse=True)
async def cleanup_channels():
    """Los canales que crea cada prueba se borran al final: el cupo de canales de la empresa 1 es compartido
    con las demás pruebas (plan con límite de canales)."""
    async with SessionLocal() as s:
        before = set((await s.scalars(select(Channel.id))).all())
    yield
    async with SessionLocal() as s:
        new = [i for i in (await s.scalars(select(Channel.id))).all() if i not in before]
        if new:
            await s.execute(delete(Conversation).where(Conversation.channel_id.in_(new)))
            await s.execute(delete(Channel).where(Channel.id.in_(new)))
            await s.commit()


async def _email_channel(c, address="ventas@concesionario.test", **extra):
    body = {"name": "Correo ventas", "address": address, "inbound": "provider",
            "smtp": {"host": "smtp.test", "port": 587, "user": address, "password": "app-pass"},
            "signature_html": "<b>Equipo de ventas</b><script>alert(1)</script>", "auto_reply": None, **extra}
    r = await c.post("/api/channels/email", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _path(url: str) -> str:
    return "/" + url.split("/", 3)[3]


# --- Unidades: limpieza de HTML y texto citado ----------------------------------------------------------------
def test_sanitize_and_strip_quoted():
    dirty = ('<p onclick="x()">Hola <b>Ana</b></p><script>steal()</script><style>p{}</style>'
             '<a href="javascript:alert(1)">mal</a><a href="https://ok.test">bien</a><img src=x onerror=alert(1)>'
             '<iframe src="https://evil"></iframe>')
    clean = mail.sanitize_html(dirty)
    for bad in ("script", "steal", "onclick", "onerror", "javascript:", "iframe", "style"):
        assert bad not in clean.lower()
    assert "<b>Ana</b>" in clean and 'href="https://ok.test"' in clean and 'rel="noopener' in clean
    body = "Sí, me interesa la camioneta.\n\nEl lun, 6 oct 2026 a las 10:00, Ventas escribió:\n> Hola, ¿te interesa?"
    assert mail.strip_quoted(body) == "Sí, me interesa la camioneta."
    assert mail.strip_quoted("Gracias\n-- \nJuan Pérez\nGerente") == "Gracias"
    assert mail.reply_subject("Cotización CX-5") == "Re: Cotización CX-5" and mail.reply_subject("RE: x") == "RE: x"
    assert mail.split_refs("<a@x> <B@Y>") == ["a@x", "b@y"]


# --- Correo de extremo a extremo -------------------------------------------------------------------------------
async def test_email_channel_providers_threading_and_replies(client):
    c = client
    ch = await _email_channel(c)
    assert ch["inbound_url"].endswith("/webhooks/email/" + ch["inbound_url"].rsplit("/", 1)[1])
    assert ch["smtp"]["has_password"] is True and "password_secret_id" not in json.dumps(ch)
    hook = _path(ch["inbound_url"])

    # 1) Postmark: HTML con script y adjunto
    pm = {"FromFull": {"Email": "Laura.Gomez@cliente.test", "Name": "Laura Gómez"},
          "ToFull": [{"Email": "ventas@concesionario.test"}], "Subject": "Cotización Isuzu D-Max",
          "TextBody": "Hola, quiero cotizar la D-Max.\n\nOn Mon, X wrote:\n> viejo",
          "HtmlBody": '<p>Hola, quiero cotizar la <b>D-Max</b>.</p><script>alert(1)</script><img src="x" onerror="y">',
          "Headers": [{"Name": "Message-ID", "Value": "<pm-1@cliente.test>"}],
          "Attachments": [{"Name": "cedula.pdf", "ContentType": "application/pdf",
                           "Content": base64.b64encode(b"%PDF-1.4 fake").decode()}]}
    r = await c.post(hook, json=pm)
    assert r.status_code == 200, r.text
    assert (await c.post(hook, json=pm)).status_code == 200  # reintento del proveedor: no duplica
    await settle()
    async with SessionLocal() as s:
        conv = await s.scalar(select(Conversation).where(Conversation.channel_id == ch["id"]))
        msgs = (await s.scalars(select(Message).where(Message.conversation_id == conv.id, Message.direction == "in"))).all()
    assert len(msgs) == 1 and msgs[0].type == "email" and msgs[0].text == "Hola, quiero cotizar la D-Max."
    detail = (await c.get(f"/api/messages/{msgs[0].id}/email")).json()
    assert detail["subject"] == "Cotización Isuzu D-Max" and detail["from"] == "laura.gomez@cliente.test"
    assert "<b>D-Max</b>" in detail["html"] and "script" not in detail["html"] and "onerror" not in detail["html"]
    assert detail["attachments"][0]["filename"] == "cedula.pdf"
    att = await c.get(f"/api/messages/{msgs[0].id}/email/attachments/0")
    assert att.status_code == 200 and att.content == b"%PDF-1.4 fake"

    # La IA respondió por SMTP en el mismo hilo
    assert sent, "el bot debía responder por correo"
    cfg, out = sent[-1]
    assert cfg["password"] == "app-pass" and cfg["host"] == "smtp.test"
    assert out["To"] == "laura.gomez@cliente.test" and out["Subject"] == "Re: Cotización Isuzu D-Max"
    assert out["In-Reply-To"] == "<pm-1@cliente.test>" and "Equipo de ventas" in out.get_body(("html",)).get_content()
    assert "script" not in out.get_body(("html",)).get_content()
    our_id = mail.clean_msgid(out["Message-ID"])

    # 2) SendGrid (MIME crudo) respondiendo nuestro correo → misma conversación
    reply = EmailMessage()
    reply["From"] = "Laura Gómez <laura.gomez@cliente.test>"
    reply["To"] = "ventas@concesionario.test"
    reply["Subject"] = "Re: Cotización Isuzu D-Max"
    reply["Message-ID"] = "<sg-2@cliente.test>"
    reply["In-Reply-To"] = f"<{our_id}>"
    reply["References"] = f"<pm-1@cliente.test> <{our_id}>"
    reply.set_content("¿Tienen financiación?\n\n> ¡Hola! ¿En qué te ayudo?")
    r = await c.post(hook, files={"email": (None, reply.as_string())}, data={"to": "ventas@concesionario.test"})
    assert r.status_code == 200, r.text
    await settle()
    async with SessionLocal() as s:
        texts = [m.text for m in (await s.scalars(select(Message).where(
            Message.conversation_id == conv.id, Message.direction == "in").order_by(Message.id))).all()]
    assert texts[-1] == "¿Tienen financiación?"

    # 3) Mailgun con firma: inválida → 401; válida → otro remitente, otra conversación
    r = await c.put(f"/api/channels/{ch['id']}/email", json={
        "name": "Correo ventas", "address": "ventas@concesionario.test", "inbound": "provider",
        "mailgun_signing_key": "mg-key", "ai_replies": False})
    assert r.status_code == 200 and r.json()["mailgun_signing"] is True
    form = {"from": "Pedro <pedro@otro.test>", "recipient": "ventas@concesionario.test", "subject": "Taller",
            "body-plain": "Necesito cita de taller", "Message-Id": "<mg-3@otro.test>",
            "timestamp": str(int(time.time())), "token": "tok"}
    bad = await c.post(hook, data=form | {"signature": "nope"})
    assert bad.status_code == 401
    sig = hmac.new(b"mg-key", f"{form['timestamp']}tok".encode(), hashlib.sha256).hexdigest()
    n_before = len(sent)
    assert (await c.post(hook, data=form | {"signature": sig})).status_code == 200
    await settle()
    convs = (await c.get("/api/conversations", params={"channel": "email"})).json()
    assert len({x["id"] for x in convs}) >= 2
    assert len(sent) == n_before  # ai_replies desactivado: el bot no respondió

    # 4) Respuesta del asesor con asunto y CC
    r = await c.post(f"/api/conversations/{conv.id}/email",
                     json={"text": "Te comparto la ficha.", "subject": "Ficha D-Max", "cc": ["gerente@concesionario.test"]})
    assert r.status_code == 200, r.text
    _cfg, out = sent[-1]
    assert out["Subject"] == "Ficha D-Max" and out["Cc"] == "gerente@concesionario.test"
    assert out["In-Reply-To"] == "<sg-2@cliente.test>"
    assert (await c.post("/webhooks/email/no-existe", json=pm)).status_code == 404


async def test_imap_polling_is_idempotent(client, monkeypatch):
    c = client
    ch = await _email_channel(c, address="taller@concesionario.test", inbound="imap",
                              imap={"host": "imap.test", "port": 993, "user": "taller@concesionario.test",
                                    "password": "imap-pass"})
    raw = EmailMessage()
    raw["From"] = "Rosa <rosa@cliente.test>"
    raw["To"] = "taller@concesionario.test"
    raw["Subject"] = "Cita"
    raw["Message-ID"] = "<imap-1@cliente.test>"
    raw.set_content("Quiero agendar mantenimiento")
    store = {7: raw.as_bytes()}
    logins = []

    class FakeIMAP:
        def __init__(self, host, port):
            assert host == "imap.test"

        def login(self, user, pwd):
            logins.append((user, pwd))

        def select(self, folder, readonly=True):
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            if cmd == "search":
                low = int(args[1].split()[1].split(":")[0])
                return "OK", [" ".join(str(u) for u in store if u >= low).encode()]
            uid = int(args[0])
            return "OK", [(f"{uid} (RFC822 {{1}}".encode(), store[uid]), b")"]

        def logout(self):
            return None

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    async with SessionLocal() as s:
        channel = await s.get(Channel, ch["id"])
        assert await mail.poll_channel(s, channel) == 1
        channel = await s.get(Channel, ch["id"])
        assert channel.settings["imap_last_uid"] == 7
        assert await mail.poll_channel(s, channel) == 0  # nada nuevo
    assert logins[0] == ("taller@concesionario.test", "imap-pass")
    convs = [x for x in (await c.get("/api/conversations", params={"channel": "email"})).json()
             if x["channel_id"] == ch["id"]]
    assert len(convs) == 1


# --- Botón flotante de WhatsApp -------------------------------------------------------------------------------
async def test_whatsapp_floating_button(client):
    c = client
    async with SessionLocal() as s:
        await s.execute(update(Channel).where(Channel.provider == "whatsapp_cloud").values(display_phone="+57 300 111 2222"))
        await s.commit()
    link = (await c.post("/api/wa-links", json={"name": "Botón web ventas", "platform": "web",
                                                "trigger_text": "Hola, vengo desde el botón del sitio"})).json()
    assert (await c.post("/api/wa-widgets", json={"name": "Vacío", "config": {"agents": []}})).status_code == 422
    r = await c.post("/api/wa-widgets", json={"name": "Sitio", "config": {
        "mode": "multi", "title": "Habla con nosotros", "allowed_domains": ["https://www.concesionario.test/"],
        "button": {"text": "Escríbenos", "color": "#25d366", "position": "left"},
        "agents": [{"name": "Ventas", "role": "Vehículos nuevos", "link_id": link["id"]},
                   {"name": "Taller", "prefill": "Hola, quiero una cita", "hours": {"days": [0, 1, 2, 3, 4], "from": "08:00", "to": "18:00"}},
                   {"name": "<img src=x onerror=1>"}],
        "show_on": {"paths_exclude": ["/checkout*"], "delay_s": 3}}})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["config"]["allowed_domains"] == ["www.concesionario.test"] and w["embed"].startswith("<script")
    js = await c.get(f"/b/{w['key']}.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    assert f"/t/l/{link['slug']}" in js.text and "https://wa.me/573001112222?text=Hola%2C%20quiero%20una%20cita" in js.text
    assert "<img src=x" not in js.text.split("var B=")[0]  # el nombre va como texto (textContent), JSON escapado
    origin = {"origin": "https://www.concesionario.test"}
    for kind in ("impression", "click", "click"):
        assert (await c.post(f"/b/{w['key']}/event", content=json.dumps({"type": kind}), headers=origin)).status_code == 200
    assert (await c.post(f"/b/{w['key']}/event", content='{"type":"click"}',
                         headers={"origin": "https://evil.test"})).status_code == 403
    listed = {x["id"]: x for x in (await c.get("/api/wa-widgets")).json()}
    assert (listed[w["id"]]["impressions"], listed[w["id"]]["clicks"], listed[w["id"]]["ctr_pct"]) == (1, 2, 200.0)
    r = await c.put(f"/api/wa-widgets/{w['id']}", json={"name": "Sitio", "is_active": False,
                                                        "config": {"agents": [{"name": "Ventas"}]}})
    assert r.status_code == 200 and (await c.get(f"/b/{w['key']}.js")).status_code == 404


# --- Chat web: opciones avanzadas -----------------------------------------------------------------------------
async def test_webchat_advanced_options(client):
    c = client
    r = await c.post("/api/channels/webchat", json={"name": "Chat sitio", "settings": {
        "open_selector": ".abrir-chat", "auto_open_s": 8, "initial_message": "Hola, vengo del sitio web",
        "clear_on_open": True, "avatar_url": "http://inseguro.test/a.png", "theme": "dark", "launcher_text": "¿Dudas?"}})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert (s["open_selector"], s["auto_open_s"], s["theme"], s["launcher_text"]) == (".abrir-chat", 8, "dark", "¿Dudas?")
    assert s["avatar_url"] is None and s["clear_on_open"] is True  # solo https
    key = r.json()["external_id"]
    js = (await c.get(f"/w/{key}.js")).text
    assert '"open_selector": ".abrir-chat"' in js and '"initial_message": "Hola, vengo del sitio web"' in js
    sess = (await c.post("/w/session", content=json.dumps({"key": key, "visitor_id": "v-opts"}))).json()
    assert sess["settings"]["auto_open_s"] == 8 and sess["settings"]["clear_on_open"] is True
    bad = await c.put(f"/api/channels/{r.json()['id']}/webchat", json={"name": "Chat sitio",
                                                                        "settings": {"open_selector": "a{x}"}})
    assert bad.json()["settings"]["open_selector"] is None


# --- /v1/messages: enrutamiento ------------------------------------------------------------------------------
async def test_v1_message_routing_options(client):
    c = client
    phone = "573109990001"
    await c.post("/webhooks/whatsapp", json=text(phone, "rt.1", "Hola, información del Onix"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]
    await c.post("/api/groups", json={"name": "Recuperación Nuevos", "description": "Leads recuperados"})
    async with SessionLocal() as s:
        luis = await s.scalar(select(Agent).where(Agent.email == "admin@test.com"))
    current = (await c.get("/api/settings/conversations")).json().get("typifications") or []
    names = [t["name"] if isinstance(t, dict) else t for t in current]
    if "Seguimiento API" not in names:  # se agrega sin quitar las tipificaciones que usan otras pruebas
        await c.put("/api/settings/conversations", json={"typifications": [*names, "Seguimiento API"]})
    key = (await c.post("/api/api-keys", json={"name": "CRM", "scopes": ["messages:send", "conversations:read"]})).json()["key"]
    api = {"Authorization": f"Bearer {key}"}
    bad = await c.post(f"/v1/conversations/{conv['id']}/messages", headers=api,
                       json={"text": "Hola", "group": "No existe"})
    assert bad.status_code == 422
    r = await c.post(f"/v1/conversations/{conv['id']}/messages", headers=api, json={
        "text": "Te contactará un asesor.", "assign": "admin@test.com", "group": "recuperación nuevos",
        "owner_agent_id": luis.id, "tags": ["api", "Onix"], "typification": "Seguimiento API"})
    assert r.status_code == 201, r.text
    async with SessionLocal() as s:
        cv = await s.get(Conversation, conv["id"])
        await s.refresh(cv, ["tag_links", "contact", "group", "typification"])
        assert cv.status == "human" and cv.assigned_agent_id == luis.id and cv.group.name == "Recuperación Nuevos"
        assert cv.contact.owner_agent_id == luis.id and cv.typification.name == "Seguimiento API"
        assert {t.tag.name for t in cv.tag_links} >= {"api", "onix"}
    r = await c.post(f"/v1/conversations/{conv['id']}/messages", headers=api, json={"text": "ok", "pause_bot": True})
    assert r.status_code == 201
    async with SessionLocal() as s:  # el grupo es de esta prueba: no debe aparecer en las demás (enrutamiento por IA)
        await s.execute(delete(Group).where(Group.organization_id == 1, Group.name == "Recuperación Nuevos"))
        await s.commit()


# --- Aislamiento -----------------------------------------------------------------------------------------------
async def test_isolation_between_organizations(client):
    c = client
    ch = await _email_channel(c, address="iso@concesionario.test")
    w = (await c.post("/api/wa-widgets", json={"name": "Iso", "config": {"agents": [{"name": "A"}]}})).json()
    async with SessionLocal() as s:
        if not await s.get(Organization, 9477):
            s.add(Organization(id=9477, name="Otra brechas", slug="otra-brechas-9477", timezone="America/Bogota"))
            await s.flush()
        if not await s.scalar(select(Agent.id).where(Agent.email == "admin9477@brechas.test")):
            s.add(Agent(organization_id=9477, email="admin9477@brechas.test", name="Admin otra", role="admin",
                        password_hash=hash_password("Clave-Segura-9477")))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as other:
        r = await other.post("/api/auth/login", json={"email": "admin9477@brechas.test", "password": "Clave-Segura-9477"})
        assert r.status_code == 200 and "access_token" in r.json(), r.text
        tok = r.json()["access_token"]
        other.headers["Authorization"] = f"Bearer {tok}"
        assert (await other.get(f"/api/channels/{ch['id']}/email")).status_code == 404
        assert all(x["id"] != w["id"] for x in (await other.get("/api/wa-widgets")).json())
        assert (await other.put(f"/api/wa-widgets/{w['id']}", json={"name": "x", "config": {"agents": [{"name": "B"}]}}
                                )).status_code == 404
    async with SessionLocal() as s:
        assert (await s.get(WaWidget, w["id"])).name == "Iso"
