"""API pública /v1: llaves, alcances, límites, idempotencia, aislamiento, mensajes, negocios, REST Hooks y Web Push."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text as sql, update

from app import push
from app.db import SessionLocal
from app.models import Agent, ApiKey, Contact, Organization, OutboundWebhook, PushSubscription, utcnow
from app.public_api import core
from app.public_api.keys import hash_key, new_key
from tests.conftest import WA, settle, text

ALL = ["contacts:read", "contacts:write", "conversations:read", "conversations:write", "messages:read",
       "messages:send", "deals:read", "deals:write", "webhooks:manage", "reports:read"]


async def _key(c, scopes=None, **kw) -> dict:
    r = await c.post("/api/api-keys", json={"name": "Integración", "scopes": scopes or ALL, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _h(key: str, **extra) -> dict:
    return {"Authorization": f"Bearer {key}", **extra}


async def test_keys_auth_and_scopes(client):
    c = client
    spec = await c.get("/v1/openapi.json")
    assert spec.status_code == 200 and "/contacts" in spec.json()["paths"] and "/webhooks" in spec.json()["paths"]
    assert (await c.get("/v1/docs")).status_code == 200
    k = await _key(c, ["contacts:read"])
    assert k["key"].startswith(k["prefix"] + "_") and k["prefix"].startswith("wak_live_")
    async with SessionLocal() as s:
        row = await s.get(ApiKey, k["id"])
        assert row.key_hash == hash_key(k["key"]) and k["key"] not in (row.key_hash, row.prefix)
    listed = {x["id"]: x for x in (await c.get("/api/api-keys")).json()}
    assert listed[k["id"]]["key"] is None  # la llave completa solo se muestra al crearla

    me = await c.get("/v1/me", headers=_h(k["key"]))
    assert me.status_code == 200 and me.json()["key"]["scopes"] == ["contacts:read"]
    assert me.headers["X-RateLimit-Limit"] == "120"

    r = await c.get("/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = await c.get("/v1/me", headers=_h(k["key"][:-1] + "X"))
    assert r.status_code == 401
    r = await c.get("/v1/deals", headers=_h(k["key"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "missing_scope"
    r = await c.post("/v1/contacts", headers=_h(k["key"]), json={"phone": "573001110000"})
    assert r.status_code == 403
    assert (await c.post("/api/api-keys", json={"name": "x", "scopes": ["todo:todo"]})).status_code == 422

    rotated = (await c.post(f"/api/api-keys/{k['id']}/rotate")).json()
    assert (await c.get("/v1/me", headers=_h(k["key"]))).status_code == 401  # la anterior ya no sirve
    assert (await c.get("/v1/me", headers=_h(rotated["key"]))).status_code == 200

    async with SessionLocal() as s:
        await s.execute(update(ApiKey).where(ApiKey.id == k["id"]).values(expires_at=utcnow() - timedelta(minutes=1)))
        await s.commit()
    r = await c.get("/v1/me", headers=_h(rotated["key"]))
    assert r.status_code == 401 and r.json()["error"]["code"] == "key_expired"

    k2 = await _key(c, ["contacts:read"])
    assert (await c.post(f"/api/api-keys/{k2['id']}/revoke")).json()["revoked_at"]
    assert (await c.get("/v1/me", headers=_h(k2["key"]))).status_code == 401


async def test_rate_limit_and_request_log(client, monkeypatch):
    c = client
    monkeypatch.setattr(core, "WINDOW_S", 3600)  # ventana larga: la prueba no depende del minuto
    k = await _key(c, ["contacts:read"], rate_limit_per_min=2)
    assert (await c.get("/v1/me", headers=_h(k["key"]))).headers["X-RateLimit-Remaining"] == "1"
    assert (await c.get("/v1/contacts", headers=_h(k["key"]))).status_code == 200
    r = await c.get("/v1/me", headers=_h(k["key"]))
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) > 0 and r.headers["X-RateLimit-Remaining"] == "0"

    await core.flush_logs()
    async with SessionLocal() as s:
        rows = (await s.execute(sql("select path, status from public.api_requests where api_key_id = :k order by id"),
                                {"k": k["id"]})).all()
    assert [tuple(r) for r in rows] == [("/v1/me", 200), ("/v1/contacts", 200), ("/v1/me", 429)]


async def test_contacts_upsert_idempotency_and_isolation(client):
    c = client
    k = await _key(c)
    body = {"phone": "+57 300 222 0001", "name": "Marta API", "tags": ["Zapier"], "stage": "prospect"}
    r = await c.post("/v1/contacts", headers=_h(k["key"], **{"Idempotency-Key": "abc-1"}), json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["created"] and created["contact"]["phone"] == "573002220001"
    assert created["contact"]["tags"] == ["zapier"] and created["contact"]["stage"] == "prospect"

    again = await c.post("/v1/contacts", headers=_h(k["key"], **{"Idempotency-Key": "abc-1"}), json=body)
    assert again.status_code == 201 and again.json() == created and again.headers["Idempotent-Replayed"] == "true"
    other = await c.post("/v1/contacts", headers=_h(k["key"], **{"Idempotency-Key": "abc-1"}),
                         json={**body, "name": "Otra"})
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"

    # Sin llave de idempotencia: el mismo teléfono actualiza (200), agrega etiquetas sin quitar las anteriores
    r = await c.post("/v1/contacts", headers=_h(k["key"]), json={"phone": "573002220001", "tags": ["vip"]})
    assert r.status_code == 200 and not r.json()["created"] and r.json()["contact"]["tags"] == ["vip", "zapier"]
    # Solo correo
    r = await c.post("/v1/contacts", headers=_h(k["key"]), json={"email": "solo@correo.com", "name": "Sin teléfono"})
    assert r.status_code == 201 and r.json()["contact"]["phone"] is None
    assert (await c.post("/v1/contacts", headers=_h(k["key"]), json={"name": "x"})).status_code == 422
    r = await c.post("/v1/contacts", headers=_h(k["key"]), json={"phone": "573002220001",
                                                                 "custom_fields": {"no_existe": 1}})
    assert r.status_code == 422 and "no_existe" in r.json()["error"]["message"]

    cid = created["contact"]["id"]
    r = await c.patch(f"/v1/contacts/{cid}", headers=_h(k["key"]), json={"tags": ["solo-esta"], "notes": "Desde API"})
    assert r.json()["tags"] == ["solo-esta"] and r.json()["notes"] == "Desde API"
    r = await c.post(f"/v1/contacts/{cid}/tags", headers=_h(k["key"]), json={"add": ["b"], "remove": ["solo-esta"]})
    assert r.json()["tags"] == ["b"]
    page1 = (await c.get("/v1/contacts", headers=_h(k["key"]), params={"limit": 1})).json()
    assert len(page1["data"]) == 1 and page1["next_cursor"]
    page2 = (await c.get("/v1/contacts", headers=_h(k["key"]), params={"limit": 1, "cursor": page1["next_cursor"]})).json()
    assert page2["data"][0]["id"] < page1["data"][0]["id"]
    assert (await c.get("/v1/contacts", headers=_h(k["key"]), params={"cursor": "%%%"})).status_code == 400

    # Otra empresa con su propia llave no ve nada de la empresa 1
    async with SessionLocal() as s:
        if not await s.get(Organization, 31):
            s.add(Organization(id=31, name="Org API 31", slug="org-api-31", timezone="America/Bogota"))
            await s.flush()
            s.add(Contact(organization_id=31, wa_id="573009990031", name="De la 31"))
        full, prefix, digest = new_key()
        s.add(ApiKey(organization_id=31, name="k31", prefix=prefix, key_hash=digest, scopes=ALL))
        await s.commit()
    assert (await c.get(f"/v1/contacts/{cid}", headers=_h(full))).status_code == 404
    names = [x["name"] for x in (await c.get("/v1/contacts", headers=_h(full))).json()["data"]]
    assert names == ["De la 31"]
    assert (await c.patch(f"/v1/contacts/{cid}", headers=_h(full), json={"name": "hack"})).status_code == 404
    convs = (await c.get("/v1/conversations", headers=_h(full))).json()["data"]
    assert convs == []


async def test_messages_conversations_and_deals(client):
    c = client
    k = await _key(c)
    phone = "573002220099"
    await c.post("/webhooks/whatsapp", json=text(phone, "api.1", "Hola, quiero info"))
    await settle()
    conv = (await c.get("/v1/conversations", headers=_h(k["key"]), params={"status": "open"})).json()
    conv = next(x for x in conv["data"] if x["contact"]["phone"] == phone)

    r = await c.post(f"/v1/conversations/{conv['id']}/messages", headers=_h(k["key"]), json={"text": "Hola desde el CRM"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "sent" and WA.sent[-1] == (phone, "Hola desde el CRM")
    detail = (await c.get(f"/v1/conversations/{conv['id']}", headers=_h(k["key"]))).json()
    assert detail["messages"][-1]["text"] == "Hola desde el CRM"
    assert (await c.post(f"/v1/conversations/{conv['id']}/messages", headers=_h(k["key"]), json={})).status_code == 422

    # Teléfono nuevo: fuera de ventana sin plantilla → 409; con plantilla → 201
    r = await c.post("/v1/messages", headers=_h(k["key"]), json={"phone": "573002220100", "text": "hola"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "outside_window"
    r = await c.post("/v1/messages", headers=_h(k["key"]), json={
        "phone": "573002220100", "template": {"name": "recordatorio", "language": "es", "values": ["lunes"]}})
    assert r.status_code == 201, r.text
    assert WA.templates_sent[-1][:2] == ("573002220100", "recordatorio")

    # Asignar y cerrar con tipificación
    async with SessionLocal() as s:
        luis = Agent(organization_id=1, email=f"api.luis.{conv['id']}@test.com", name="Luis API", role="agent")
        s.add(luis)
        await s.commit()
        luis_id = luis.id
    r = await c.post(f"/v1/conversations/{conv['id']}/assign", headers=_h(k["key"]), json={"agent_id": luis_id})
    assert r.json()["assigned_agent"]["id"] == luis_id and r.json()["status"] == "human"
    async with SessionLocal() as s:
        actor = await s.scalar(sql("select actor_type from public.conversation_events where conversation_id = :c "
                                   "and event_type = 'assigned' order by occurred_at desc limit 1"), {"c": conv["id"]})
    assert actor == "api"
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Consulta resuelta"]})
    assert (await c.post(f"/v1/conversations/{conv['id']}/close", headers=_h(k["key"]),
                         json={"typification": "No existe"})).status_code == 422
    r = await c.post(f"/v1/conversations/{conv['id']}/close", headers=_h(k["key"]), json={"typification": "Venta"})
    assert r.json()["status"] == "closed" and r.json()["typification"] == "Venta"

    # Negocios
    r = await c.post("/v1/deals", headers=_h(k["key"], **{"Idempotency-Key": "deal-1"}),
                     json={"name": "CX-5 Grand Touring", "phone": phone, "amount": 150000000, "currency": "cop"})
    assert r.status_code == 201, r.text
    deal = r.json()
    assert deal["source"] == "api" and deal["currency"] == "COP" and deal["status"] == "open"
    replay = await c.post("/v1/deals", headers=_h(k["key"], **{"Idempotency-Key": "deal-1"}),
                          json={"name": "CX-5 Grand Touring", "phone": phone, "amount": 150000000, "currency": "cop"})
    assert replay.json()["id"] == deal["id"]  # no se duplicó
    assert (await c.patch(f"/v1/deals/{deal['id']}", headers=_h(k["key"]), json={"stage": "no-existe"})).status_code == 422
    r = await c.post(f"/v1/deals/{deal['id']}/won", headers=_h(k["key"]), json={"amount": 149000000})
    assert r.json()["status"] == "won" and r.json()["amount"] == 149000000
    won = (await c.get("/v1/deals", headers=_h(k["key"]), params={"status": "won"})).json()["data"]
    assert deal["id"] in [d["id"] for d in won]
    summary = (await c.get("/v1/reports/summary", headers=_h(k["key"]))).json()
    assert summary["deals"]["won"] >= 1 and "conversations" in summary


async def test_rest_hooks(client):
    c = client
    k = await _key(c, ["webhooks:manage"])
    r = await c.post("/v1/webhooks", headers=_h(k["key"], **{"X-Connector": "zapier"}),
                     json={"url": "https://hooks.zapier.com/hooks/standard/1/abc", "events": ["message.new"]})
    assert r.status_code == 201, r.text
    hook = r.json()
    assert hook["source"] == "zapier" and len(hook["secret"]) == 48
    async with SessionLocal() as s:
        row = await s.get(OutboundWebhook, hook["id"])
        assert row.api_key_id == k["id"] and row.events == ["message.new"] and row.signing_secret_id
    assert [h["id"] for h in (await c.get("/v1/webhooks", headers=_h(k["key"]))).json()["data"]] == [hook["id"]]
    assert (await c.post("/v1/webhooks", headers=_h(k["key"]), json={"url": "http://x.com", "events": []})).status_code == 422
    assert (await c.post("/v1/webhooks", headers=_h(k["key"]),
                         json={"url": "https://x.com", "events": ["no.existe"]})).status_code == 422
    assert (await c.post("/v1/webhooks", headers=_h(k["key"], **{"X-Connector": "otro"}),
                         json={"url": "https://x.com"})).status_code == 422
    sample = (await c.get("/v1/webhooks/sample/message.new", headers=_h(k["key"]))).json()
    assert isinstance(sample, list) and sample[0]["direction"] == "in"
    events = {e["event"] for e in (await c.get("/v1/events", headers=_h(k["key"]))).json()}
    assert "conversation.closed" in events

    panel = (await c.post("/api/outbound-webhooks", json={"name": "Panel", "url": "https://panel.example.com/h",
                                                          "events": []})).json()
    assert (await c.delete(f"/v1/webhooks/{panel['id']}", headers=_h(k["key"]))).status_code == 404
    assert (await c.delete(f"/v1/webhooks/{hook['id']}", headers=_h(k["key"]))).json()["deleted"]
    async with SessionLocal() as s:
        assert await s.get(OutboundWebhook, hook["id"]) is None

    # Revocar la llave desactiva sus suscripciones
    r = await c.post("/v1/webhooks", headers=_h(k["key"], **{"X-Connector": "n8n"}), json={"url": "https://n8n.example.com/w"})
    await c.post(f"/api/api-keys/{k['id']}/revoke")
    async with SessionLocal() as s:
        assert (await s.get(OutboundWebhook, r.json()["id"])).active is False
    await c.delete(f"/api/outbound-webhooks/{panel['id']}")


@pytest.fixture
def sent_push(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def fake(subscription, payload):
        import json

        if subscription["endpoint"].endswith("/gone"):
            raise push.PushGone("410")
        sent.append((subscription["endpoint"], json.loads(payload)))

    monkeypatch.setattr(push, "SENDER", fake)
    return sent


async def test_push_recipients(client, sent_push):
    c = client
    phone = "573002220200"
    await c.post("/webhooks/whatsapp", json=text(phone, "push.1", "Hola, necesito ayuda"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]
    async with SessionLocal() as s:
        admin = await s.scalar(select(Agent).where(Agent.email == "admin@test.com"))
        maria = Agent(organization_id=1, email="maria.push@test.com", name="María", role="agent")
        s.add(maria)
        await s.flush()
        s.add_all([PushSubscription(organization_id=1, agent_id=maria.id, endpoint="https://push.example/maria",
                                    p256dh="k", auth="a"),
                   PushSubscription(organization_id=1, agent_id=maria.id, endpoint="https://push.example/gone",
                                    p256dh="k", auth="a"),
                   PushSubscription(organization_id=1, agent_id=admin.id, endpoint="https://push.example/admin",
                                    p256dh="k", auth="a")])
        await s.commit()
        maria_id, admin_id = maria.id, admin.id

    # El administrador le transfiere la conversación a María → aviso a María (una sola vez)
    # (el hub ya llama a push.on_event al publicar; la llamada explícita no debe duplicar el aviso)
    data = (await c.post(f"/api/conversations/{conv['id']}/transfer", json={"agent_id": maria_id})).json()
    await settle()
    await push.handle("conversation.updated", data, 1)
    await push.handle("conversation.updated", data, 1)
    to_maria = [n for e, n in sent_push if e == "https://push.example/maria"]
    assert len(to_maria) == 1 and to_maria[0]["url"] == f"/conversaciones?id={conv['id']}"
    async with SessionLocal() as s:
        assert await s.scalar(select(PushSubscription).where(PushSubscription.endpoint.like("%/gone"))) is None

    # Mensaje del cliente en su conversación y María no está conectada → aviso
    msg = {"conversation_id": conv["id"], "direction": "in", "type": "text", "text": "¿Siguen ahí?"}
    assert await push.handle("message.new", msg, 1) == 1 and sent_push[-1][1]["body"] == "¿Siguen ahí?"
    assert await push.handle("message.new", {**msg, "direction": "out"}, 1) == 0
    # Preferencia apagada
    async with SessionLocal() as s:
        await s.execute(update(PushSubscription).where(PushSubscription.agent_id == maria_id)
                        .values(preferences={"assigned": True, "message_assigned": False}))
        await s.commit()
    assert await push.handle("message.new", msg, 1) == 0

    # El administrador se toma una conversación él mismo → sin aviso
    before = len(sent_push)
    data = (await c.post(f"/api/conversations/{conv['id']}/assign")).json()
    assert data["assigned_agent"]["id"] == admin_id
    await settle()
    assert await push.handle("conversation.updated", data, 1) == 0 and len(sent_push) == before
    # Llamada entrante para los asesores a quienes suena
    assert await push.handle("call.incoming", {"id": 9, "targets": [admin_id], "conversation_id": conv["id"]}, 1) == 1
    assert sent_push[-1][0] == "https://push.example/admin" and sent_push[-1][1]["title"] == "Llamada entrante"
    # on_event no bloquea ni lanza
    task = await push.on_event("message.new", msg, 1)  # ahora la tiene el administrador, desconectado del panel
    assert task is not None and await task == 1 and sent_push[-1][0] == "https://push.example/admin"
    assert await push.on_event("agent.presence", {}, 1) is None

    # Endpoints del panel
    r = await c.post("/api/push/subscriptions", json={"endpoint": "https://push.example/panel",
                                                      "keys": {"p256dh": "k", "auth": "a"},
                                                      "preferences": {"call": False}})
    assert r.json()["preferences"] == {"assigned": True, "message_assigned": True, "call": False,
                                       "new_conversation": False}
    assert (await c.post("/api/push/subscriptions", json={"endpoint": "http://x", "keys": {"p256dh": "k", "auth": "a"}})
            ).status_code == 422
    assert (await c.get("/api/push/vapid-public-key")).json()["enabled"] is False
    assert (await c.request("DELETE", "/api/push/subscriptions", json={"endpoint": "https://push.example/panel"})).json()
