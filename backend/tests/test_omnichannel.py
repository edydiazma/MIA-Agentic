"""Omnicanal: Messenger, Instagram y chat web en la misma bandeja, flujos e IA (Graph API simulada)."""

import json

import httpx
import pytest
from sqlalchemy import select, update

from app.channels import meta as meta_mod
from app.channels import webchat as live
from app.db import SessionLocal
from app.models import Attribution, Channel, ContactIdentity, Conversation, InboundEvent, Organization, Plan
from tests.conftest import WA, settle, text

graph_posts: list[tuple[str, dict]] = []


class FakeGraph:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, params=None, data=None, files=None):
        graph_posts.append((url, {"json": json, "params": params, "data": data}))
        if url.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        if url.endswith("/message_attachments"):
            return httpx.Response(200, json={"attachment_id": "ATT1"})
        return httpx.Response(200, json={"recipient_id": (json or {}).get("recipient", {}).get("id"),
                                         "message_id": f"m_out_{len(graph_posts)}"})

    async def get(self, url, params=None):
        if "cdn.example" in url:
            return httpx.Response(200, content=b"\x89PNG fake", headers={"content-type": "image/png"})
        return httpx.Response(200, json={"name": "Laura Gómez", "profile_pic": "https://cdn.example/p.jpg",
                                         "username": "laura.g"})


@pytest.fixture(autouse=True)
def fake_graph(monkeypatch):
    monkeypatch.setattr(meta_mod, "http_client", lambda timeout=30: FakeGraph())
    graph_posts.clear()


def _sends(recipient: str) -> list[dict]:
    return [p["json"] for url, p in graph_posts if url.endswith("/messages") and p["json"]
            and p["json"].get("recipient", {}).get("id") == recipient and "message" in p["json"]]


def page_event(obj: str, entry_id: str, sender: str, mid: str, message: dict | None = None, **extra) -> dict:
    ev = {"sender": {"id": sender}, "recipient": {"id": entry_id}, "timestamp": 1760000000000, **extra}
    if message is not None:
        ev["message"] = {"mid": mid, **message}
    return {"object": obj, "entry": [{"id": entry_id, "time": 1760000000000, "messaging": [ev]}]}


async def _channels(c):
    r = await c.post("/api/channels/meta", json={"provider": "messenger", "name": "Página FB", "page_id": "PAGE1",
                                                 "access_token": "PAGETOKEN"})
    assert r.status_code == 200, r.text
    fb = r.json()
    r = await c.post("/api/channels/meta", json={"provider": "instagram", "name": "IG tienda", "page_id": "PAGE1",
                                                 "ig_account_id": "IG1", "access_token": "PAGETOKEN"})
    assert r.status_code == 200, r.text
    return fb, r.json()


async def test_messenger_and_instagram_pipeline(client):
    c = client
    fb, ig = await _channels(c)
    assert fb["provider"] == "messenger" and fb["status"] == "active"
    assert any(u.endswith("/PAGE1/subscribed_apps") for u, _ in graph_posts)
    assert (await c.post("/api/channels/meta", json={"provider": "messenger", "name": "x", "page_id": "PAGE1",
                                                     "access_token": "t"})).status_code == 409

    # Messenger: mensaje desde un anuncio (referral) → contacto por PSID, conversación, atribución y respuesta IA
    payload = page_event("page", "PAGE1", "PSID-1", "m_in_1", {"text": "Hola, info del Onix"},
                         referral={"source": "ADS", "type": "OPEN_THREAD", "ad_id": "AD-FB-1",
                                   "ads_context_data": {"ad_title": "Onix 2027"}})
    r = await c.post("/webhooks/whatsapp", json=payload)
    assert r.status_code == 200
    await settle(0.8)
    replies = _sends("PSID-1")
    assert replies and replies[-1]["message"]["text"] == "¡Hola! ¿En qué te ayudo?"
    assert replies[-1]["messaging_type"] == "RESPONSE"
    async with SessionLocal() as s:
        ident = await s.scalar(select(ContactIdentity).where(ContactIdentity.external_id == "PSID-1"))
        assert ident.provider == "messenger" and ident.channel_id == fb["id"] and ident.username == "laura.g"
        conv = await s.scalar(select(Conversation).where(Conversation.contact_id == ident.contact_id))
        assert conv.channel_id == fb["id"] and conv.ad_source_id == "AD-FB-1" and conv.ad_headline == "Onix 2027"
        attr = await s.scalar(select(Attribution).where(Attribution.conversation_id == conv.id))
        assert (attr.channel, attr.ad_id, attr.matched_by) == ("messenger", "AD-FB-1", "ctwa_referral")
        ev = await s.scalar(select(InboundEvent).where(InboundEvent.source == "messenger")
                            .order_by(InboundEvent.id.desc()).limit(1))
        assert ev.organization_id == conv.organization_id

    convs = (await c.get("/api/conversations", params={"q": "laura.g"})).json()
    assert convs and convs[0]["channel_provider"] == "messenger" and convs[0]["contact"]["wa_id"] is None
    assert convs[0]["contact"]["name"] == "Laura Gómez" and convs[0]["window_open"]
    ids = (await c.get(f"/api/contacts/{convs[0]['contact']['id']}/identities")).json()
    assert [(i["provider"], i["external_id"]) for i in ids] == [("messenger", "PSID-1")]

    # Reintento de Meta: no duplica; eco de la página y eventos de otra página: se ignoran
    await c.post("/webhooks/whatsapp", json=payload)
    await c.post("/webhooks/whatsapp", json=page_event("page", "PAGE1", "PAGE1", "m_echo", {"text": "eco",
                                                                                          "is_echo": True}))
    await c.post("/webhooks/whatsapp", json=page_event("page", "OTRA", "PSID-9", "m_x", {"text": "hola"}))
    await settle()
    assert len(_sends("PSID-1")) == len(replies)
    assert not _sends("PSID-9")

    # El asesor responde desde la bandeja por Messenger; luego "leído" por marca de agua
    conv_id = convs[0]["id"]
    r = await c.post(f"/api/conversations/{conv_id}/messages", json={"text": "Soy Carolina, te ayudo"})
    assert r.status_code == 200 and r.json()["status"] == "sent", r.text
    assert _sends("PSID-1")[-1]["message"]["text"] == "Soy Carolina, te ayudo"
    await c.post("/webhooks/whatsapp", json=page_event("page", "PAGE1", "PSID-1", "", None,
                                                       read={"watermark": 9999999999999}))
    await settle(0.2)
    msgs = (await c.get(f"/api/conversations/{conv_id}/messages")).json()
    assert any(m["status"] == "read" for m in msgs if m["direction"] == "out")
    # Plantillas: solo WhatsApp
    r = await c.post(f"/api/conversations/{conv_id}/template", json={"name": "promo", "language": "es"})
    assert r.status_code == 409

    # Instagram: imagen adjunta (se descarga del CDN) → la IA ve la imagen y responde por Instagram
    await c.post("/webhooks/whatsapp", json=page_event(
        "instagram", "IG1", "IGSID-7", "ig_in_1",
        {"attachments": [{"type": "image", "payload": {"url": "https://cdn.example/img.png"}}]}))
    await settle(0.8)
    ig_replies = _sends("IGSID-7")
    assert ig_replies and ig_replies[-1]["message"]["text"] == "Veo tu imagen 👀"
    async with SessionLocal() as s:
        ident = await s.scalar(select(ContactIdentity).where(ContactIdentity.external_id == "IGSID-7"))
        conv = await s.scalar(select(Conversation).where(Conversation.contact_id == ident.contact_id))
        assert conv.channel_id == ig["id"]
        attr = await s.scalar(select(Attribution).where(Attribution.conversation_id == conv.id))
        assert attr.channel == "instagram" and attr.matched_by == "none"

    # WhatsApp sigue igual
    await c.post("/webhooks/whatsapp", json=text("573150009999", "omni.wa.1", "hola"))
    await settle()
    assert WA.sent[-1] == ("573150009999", "¡Hola! ¿En qué te ayudo?")

    # Quick replies de un flujo en Messenger (botones → respuestas rápidas)
    from app.channels.meta import MetaClient

    async with SessionLocal() as s:
        ch = await s.get(Channel, fb["id"])
    mc = MetaClient(ch, "PAGETOKEN", "PSID-1")
    await mc.send_buttons(None, "¿Qué te interesa?", [{"id": "a", "title": "Comprar"}, {"id": "b", "title": "Taller"}])
    qr = _sends("PSID-1")[-1]["message"]["quick_replies"]
    assert [q["title"] for q in qr] == ["Comprar", "Taller"]


async def test_webchat_widget_and_isolation(client):
    c = client
    r = await c.post("/api/channels/webchat", json={"name": "Chat sitio", "settings": {
        "title": "Hablemos", "allowed_domains": ["https://www.tienda.com/"], "color": "#123456"}})
    assert r.status_code == 200, r.text
    ch = r.json()
    key = ch["external_id"]
    assert ch["settings"]["allowed_domains"] == ["www.tienda.com"] and key in r.json()["embed"]
    js = await c.get(f"/w/{key}.js")
    assert js.status_code == 200 and key in js.text and "/w/session" in js.text

    origin = {"origin": "https://www.tienda.com"}
    assert (await c.post("/w/session", content=json.dumps({"key": key}),
                         headers={"origin": "https://malo.com"})).status_code == 403
    s1 = (await c.post("/w/session", content=json.dumps({"key": key, "visitor_id": "vis-1"}), headers=origin)).json()
    s2 = (await c.post("/w/session", content=json.dumps({"key": key, "visitor_id": "vis-2", "name": "Pedro",
                                                         "email": "pedro@x.com"}), headers=origin)).json()
    assert s1["settings"]["title"] == "Hablemos" and s1["token"] != s2["token"]

    r = await c.post("/w/messages", content=json.dumps({"key": key, "token": s1["token"], "text": "Hola, ¿precio?"}),
                     headers=origin)
    assert r.status_code == 200, r.text
    await settle(0.8)
    got = (await c.get("/w/messages", params={"key": key, "token": s1["token"]})).json()
    assert [m["sender"] for m in got] == ["visitor", "bot"] and got[1]["text"] == "¡Hola! ¿En qué te ayudo?"

    # El asesor responde desde la bandeja: llega al widget del visitante 1, nunca al 2
    conv = next(x for x in (await c.get("/api/conversations", params={"channel": "webchat"})).json()
                if x["contact"]["name"] is None)
    assert conv["channel_provider"] == "webchat" and conv["window_open"]
    ev = live.subscribe(conv["id"])
    r = await c.post(f"/api/conversations/{conv['id']}/messages", json={"text": "Te atiende Carolina"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert ev.is_set()
    live.unsubscribe(conv["id"], ev)
    got = (await c.get("/w/messages", params={"key": key, "token": s1["token"], "after": got[-1]["id"]})).json()
    assert [(m["sender"], m["text"]) for m in got] == [("agent", "Te atiende Carolina")]
    assert (await c.get("/w/messages", params={"key": key, "token": s2["token"]})).json() == []
    assert (await c.get("/w/messages", params={"key": key, "token": "falso"})).status_code == 401

    # Formulario previo: el contacto del visitante 2 ya tiene nombre y correo; retomar la sesión con el token
    again = (await c.post("/w/session", content=json.dumps({"key": key, "token": s2["token"]}), headers=origin)).json()
    assert again["token"] == s2["token"]
    async with SessionLocal() as s:
        ident = await s.scalar(select(ContactIdentity).where(ContactIdentity.external_id == "vis-2"))
        conv2 = await s.scalar(select(Conversation).where(Conversation.contact_id == ident.contact_id))
        assert conv2 is None  # aún no escribe: no hay conversación vacía
    contact = (await c.get(f"/api/contacts/{ident.contact_id}")).json()
    assert contact["name"] == "Pedro" and contact["email"] == "pedro@x.com"

    # Imagen del visitante
    r = await c.post("/w/messages", data={"key": key, "token": s2["token"]},
                     files={"file": ("foto.png", b"\x89PNG data", "image/png")}, headers=origin)
    assert r.status_code == 200 and r.json()["media_url"].startswith(f"/w/media/{r.json()['id']}")
    media = await c.get(r.json()["media_url"])
    assert media.status_code == 200 and media.content == b"\x89PNG data"
    assert (await c.get(f"/w/media/{r.json()['id']}", params={"key": key, "token": s1["token"]})).status_code == 404


async def test_plan_gate(client):
    c = client
    async with SessionLocal() as s:
        team = await s.scalar(select(Plan.id).where(Plan.key == "team"))
        original = await s.scalar(select(Organization.plan_id).where(Organization.id == 1))
        await s.execute(update(Organization).where(Organization.id == 1).values(plan_id=team))
        await s.commit()
    try:
        r = await c.post("/api/channels/webchat", json={"name": "No permitido"})
        assert r.status_code == 402 and "Omnicanal" in r.json()["detail"]
    finally:
        async with SessionLocal() as s:
            await s.execute(update(Organization).where(Organization.id == 1).values(plan_id=original))
            await s.commit()
