"""CRM: negocios, conexión OAuth (tokens en Vault), sincronización bidireccional sin eco y adaptador Salesforce."""

import base64
import uuid
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.crm import hubspot, salesforce
from app.crm.salesforce import SalesforceAdapter
from app.db import SessionLocal
from app.models import ContactChange, ConversationEvent, IntegrationConnection, IntegrationOutbox
from app.routers.integrations import verify_hubspot_signature
from app.secrets_vault import get_secret
from tests.conftest import settle, text

PHONE = "573117770001"


class FakeHubSpot:
    """Servidor HubSpot en memoria (solo lo que usa el adaptador)."""

    def __init__(self):
        self.contacts: dict[str, dict] = {}
        self.deals: dict[str, dict] = {}
        self.notes: list[dict] = []
        self.assoc: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
    def _new(self) -> str:
        # Únicos en toda la sesión (la conexión de HubSpot sobrevive entre pruebas y pytest puede importar este
        # módulo dos veces, con contadores separados): aleatorios de 12 dígitos
        return str(uuid.uuid4().int % 10**12)

    @staticmethod
    def _stamp(dt: datetime | None = None) -> str:
        return (dt or datetime.now(UTC)).isoformat().replace("+00:00", "Z")

    async def send(self, method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        path = urlparse(url).path
        self.calls.append((method, path))
        if path == "/oauth/v1/token":
            return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 1800})
        if path == "/account-info/v3/details":
            return httpx.Response(200, json={"portalId": 4242})
        if path.startswith("/oauth/v1/refresh_tokens/"):
            return httpx.Response(204)
        for obj, store, mod in (("contacts", self.contacts, "lastmodifieddate"),
                                ("deals", self.deals, "hs_lastmodifieddate")):
            base = f"/crm/v3/objects/{obj}"
            if path == f"{base}/search":
                results = []
                for group in json_body["filterGroups"]:
                    f = group["filters"][0]
                    for rid, props in store.items():
                        if f["propertyName"] == mod:
                            since = datetime.fromtimestamp(int(f["value"]) / 1000, UTC)
                            when = datetime.fromisoformat(props[mod].replace("Z", "+00:00"))
                            if when > since:
                                results.append({"id": rid, "properties": dict(props)})
                        elif str(props.get(f["propertyName"])) == f["value"]:
                            results.append({"id": rid, "properties": dict(props)})
                return httpx.Response(200, json={"results": results[: json_body.get("limit", 100)]})
            if path == base and method == "POST":
                rid = self._new()
                store[rid] = {k: str(v) for k, v in json_body["properties"].items()} | {mod: self._stamp()}
                return httpx.Response(201, json={"id": rid})
            if path.startswith(base + "/") and method == "PATCH":
                rid = path.rsplit("/", 1)[1]
                store[rid].update({k: str(v) for k, v in json_body["properties"].items()} | {mod: self._stamp()})
                return httpx.Response(200, json={"id": rid})
        if path.startswith("/crm/v4/objects/deals/") and method == "PUT":
            parts = path.split("/")
            self.assoc.append((parts[5], parts[-1]))
            return httpx.Response(200, json={})
        if path == "/crm/v3/objects/notes":
            self.notes.append(json_body)
            return httpx.Response(201, json={"id": self._new()})
        return httpx.Response(404, json={"message": f"no fake for {method} {path}"})

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT") and not c[1].endswith("/search")]


@pytest.fixture
def fake_hubspot(monkeypatch):
    fake = FakeHubSpot()
    monkeypatch.setattr(hubspot, "send", fake.send)
    s = get_settings()
    monkeypatch.setattr(s, "hubspot_client_id", "cid")
    monkeypatch.setattr(s, "hubspot_client_secret", "csecret")
    monkeypatch.setattr(s, "public_base_url", "https://api.test")
    monkeypatch.setattr(s, "frontend_base_url", "https://panel.test")
    return fake


async def _conversation(c, phone=PHONE, name="Laura Pérez"):
    await c.post("/webhooks/whatsapp", json=text(phone, f"crm.{phone}", "hola, quiero cotizar", name=name))
    await settle()
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_deals_crud_board_and_win_event(client):
    c = client
    conv = await _conversation(c, "573117770009", "Pedro Gil")
    r = await c.post("/api/deals", json={"contact_id": conv["contact"]["id"], "conversation_id": conv["id"],
                                         "name": "Onix Premier", "amount": 89900000})
    assert r.status_code == 200, r.text
    deal = r.json()
    assert deal["stage"] == "new" and deal["status"] == "open" and deal["owner"]["name"]

    board = (await c.get("/api/deals/board")).json()
    assert [s["key"] for s in board["stages"]][:2] == ["new", "qualified"]
    assert any(d["id"] == deal["id"] for d in board["columns"]["new"])
    assert board["totals"]["new"] >= 89900000

    assert (await c.put(f"/api/deals/{deal['id']}", json={"stage": "nope"})).status_code == 422
    r = await c.put(f"/api/deals/{deal['id']}", json={"stage": "proposal", "amount": 87000000})
    assert r.json()["stage"] == "proposal" and r.json()["amount"] == 87000000
    r = await c.post(f"/api/deals/{deal['id']}/win", json={"amount": 88000000})
    won = r.json()
    assert won["status"] == "won" and won["stage"] == "won" and won["closed_at"] and won["amount"] == 88000000
    async with SessionLocal() as s:
        ev = (await s.scalars(select(ConversationEvent).where(
            ConversationEvent.conversation_id == conv["id"], ConversationEvent.event_type == "deal_won"))).first()
    assert ev and ev.payload["deal_id"] == deal["id"] and ev.payload["amount"] == 88000000

    r = await c.post(f"/api/deals/{deal['id']}/reopen")
    assert r.json()["status"] == "open" and r.json()["closed_at"] is None
    r = await c.post(f"/api/deals/{deal['id']}/lose", json={"reason": "Precio"})
    assert r.json()["status"] == "lost" and r.json()["lost_reason"] == "Precio"
    assert (await c.get("/api/deals", params={"status": "lost", "q": "Onix"})).json()[0]["id"] == deal["id"]
    bad = await c.put("/api/deals/pipelines", json={"pipelines": {"default": {"stages": [{"key": "a"}]}}})
    assert bad.status_code == 422


async def test_hubspot_oauth_push_and_pull_without_echo(client, fake_hubspot):
    c = client
    conv = await _conversation(c)
    contact_id = conv["contact"]["id"]
    await c.put(f"/api/contacts/{contact_id}", json={"email": "laura@example.com", "stage": "prospect"})
    deal = (await c.post("/api/deals", json={"contact_id": contact_id, "conversation_id": conv["id"],
                                             "name": "Tracker LTZ", "amount": 124900000})).json()

    # 1) OAuth: connect → callback con state firmado → tokens en Vault
    url = (await c.get("/api/integrations/hubspot/connect")).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["https://api.test/api/integrations/hubspot/callback"]
    r = await c.get("/api/integrations/hubspot/callback", params={"code": "abc", "state": q["state"][0]},
                    follow_redirects=False)
    assert r.status_code == 302 and "connected=1" in r.headers["location"]
    assert (await c.get("/api/integrations/hubspot/callback",
                        params={"code": "abc", "state": "manipulado"}, follow_redirects=False)).headers[
        "location"].count("error=") == 1
    async with SessionLocal() as s:
        conn = await s.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "hubspot",
                                                                  IntegrationConnection.organization_id == 1))
        assert await get_secret(s, conn.access_token_secret_id) == "at-1"
        assert await get_secret(s, conn.refresh_token_secret_id) == "rt-1"
        assert conn.external_account_id == "4242"
    status = {x["provider"]: x for x in (await c.get("/api/integrations/crm")).json()}
    assert status["hubspot"]["connected"] and status["hubspot"]["via_oauth"]

    # 2) Push: contacto + negocio asociado
    r = await c.post("/api/integrations/hubspot/sync")
    assert r.status_code == 200, r.text
    hs_contact = next(p for p in fake_hubspot.contacts.values() if p.get("phone") == f"+{PHONE}")
    assert hs_contact["firstname"] == "Laura" and hs_contact["lastname"] == "Pérez"
    assert hs_contact["email"] == "laura@example.com" and hs_contact["lifecyclestage"] == "marketingqualifiedlead"
    hs_deal = next(d for d in fake_hubspot.deals.values() if d["dealname"] == "Tracker LTZ")
    assert hs_deal["dealstage"] == "appointmentscheduled"
    assert fake_hubspot.assoc, "el negocio quedó asociado al contacto"
    pushed = (await c.get(f"/api/deals/{deal['id']}")).json()["crm_links"]
    assert pushed[0]["provider"] == "hubspot"

    # 3) Reenviar lo mismo: el hash evita llamadas de escritura
    writes_before = len(fake_hubspot.writes())
    from app.crm.sync import enqueue_contact

    async with SessionLocal() as s:
        await enqueue_contact(s, contact_id)
    await c.post("/api/integrations/hubspot/sync")
    assert len(fake_hubspot.writes()) == writes_before
    outbox = (await c.get("/api/integrations/hubspot/outbox")).json()
    assert any(o["status"] == "skipped" and o["entity_type"] == "contact" for o in outbox)

    # 4) Pull: cambio en HubSpot → se aplica con fuente 'crm' y NO vuelve como eco
    rid = next(k for k, p in fake_hubspot.contacts.items() if p.get("phone") == f"+{PHONE}")
    fake_hubspot.contacts[rid].update({"firstname": "Laurita", "email": "laurita@example.com",
                                       "lastmodifieddate": FakeHubSpot._stamp(datetime.now(UTC) + timedelta(seconds=5))})
    writes_before = len(fake_hubspot.writes())
    r = await c.post("/api/integrations/hubspot/sync")
    assert r.json()["result"]["pull"]["applied"] >= 1, r.json()
    contact = (await c.get(f"/api/contacts/{contact_id}")).json()
    assert contact["name"] == "Laurita Pérez" and contact["email"] == "laurita@example.com"
    async with SessionLocal() as s:
        sources = {ch.source for ch in (await s.scalars(select(ContactChange).where(
            ContactChange.contact_id == contact_id, ContactChange.field_key == "email"))).all()}
    assert "crm" in sources
    await c.post("/api/integrations/hubspot/sync")  # el contacto cambió localmente: se encola pero no se reenvía
    assert len(fake_hubspot.writes()) == writes_before

    # 5) Nota al cerrar la conversación
    # La tipificación debe existir y estar activa (otras pruebas reemplazan la lista de la empresa 1)
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Consulta resuelta",
                                                                      "Cotización enviada", "Reclamo"]})
    r = await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Cotización enviada"})
    assert r.status_code == 200, r.text
    # Cada sincronización procesa hasta 200 filas de la cola; con la empresa 1 compartida puede haber más pendientes
    for _ in range(10):
        await c.post("/api/integrations/hubspot/sync")
        if fake_hubspot.notes:
            break
    assert fake_hubspot.notes and "Tipificación: Cotización enviada" in fake_hubspot.notes[-1]["properties"]["hs_note_body"]

    # 6) Desconectar borra secretos y vínculos
    assert (await c.delete("/api/integrations/hubspot")).status_code == 200
    async with SessionLocal() as s:
        assert not await s.scalar(select(IntegrationConnection.id).where(IntegrationConnection.provider == "hubspot",
                                                                         IntegrationConnection.organization_id == 1))
        assert await s.scalar(select(IntegrationOutbox.id).limit(1))  # la bitácora se conserva


async def test_hubspot_private_token_and_backoff(client, fake_hubspot, monkeypatch):
    c = client
    assert (await c.post("/api/integrations/hubspot/token", json={"token": "pat-123"})).json()["connected"]
    conv = await _conversation(c, "573117770002", "Ana Ruiz")

    async def failing(method, url, **kw):
        return httpx.Response(503, text="temporalmente fuera de servicio")

    monkeypatch.setattr(hubspot, "send", lambda *a, **k: failing(*a, **k) if a[0] != "GET" else fake_hubspot.send(*a, **k))
    await c.post("/api/integrations/hubspot/sync")
    outbox = (await c.get("/api/integrations/hubspot/outbox", params={"status": "pending"})).json()
    row = next(o for o in outbox if o["entity_type"] == "contact" and o["entity_id"] == conv["contact"]["id"])
    assert row["attempts"] == 1 and "503" in row["error"]
    assert datetime.fromisoformat(row["next_attempt_at"]) > datetime.now(UTC) + timedelta(seconds=30)
    await c.delete("/api/integrations/hubspot")


async def test_salesforce_adapter_upsert_path(monkeypatch):
    calls = []

    async def send(method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        path = urlparse(url).path
        calls.append((method, path, json_body, params))
        if path.endswith("/query"):
            return httpx.Response(200, json={"records": [], "done": True})
        if path.endswith("/sobjects/Contact"):
            return httpx.Response(201, json={"id": "003A"})
        if path.endswith("/sobjects/Opportunity"):
            return httpx.Response(201, json={"id": "006B"})
        if path.endswith("/sobjects/OpportunityContactRole") or path.endswith("/sobjects/Task"):
            return httpx.Response(201, json={"id": "00X"})
        return httpx.Response(404, text="?")

    monkeypatch.setattr(salesforce, "send", send)
    sf = SalesforceAdapter("tok", "https://acme.my.salesforce.com", "Contact")
    assert await sf.find_contact("o'neil@x.com", "+573001") is None
    assert "o\\'neil@x.com" in calls[0][3]["q"]  # SOQL escapado
    cid = await sf.upsert_contact(None, {"FirstName": "Ana", "Email": "a@x.com"})
    assert cid == "003A" and calls[-1][2]["LastName"] == "Ana"  # LastName es obligatorio
    oid = await sf.upsert_deal(None, {"Name": "Onix", "Amount": 1}, cid)
    assert oid == "006B"
    opp = next(c for c in calls if c[1].endswith("/sobjects/Opportunity"))
    assert opp[2]["CloseDate"] and opp[2]["StageName"] == "Prospecting"
    assert any(c[1].endswith("/OpportunityContactRole") and c[2]["ContactId"] == "003A" for c in calls)
    await sf.add_note(cid, "Resumen", oid)
    task = calls[-1][2]
    assert task["WhoId"] == "003A" and task["WhatId"] == "006B" and task["Status"] == "Completed"
    assert calls[0][1].startswith("/services/data/v61.0")


def test_hubspot_webhook_signature():
    body = json.dumps([{"portalId": 1, "objectId": 2}]).encode()
    ts = str(int(time.time() * 1000))
    uri = "https://api.test/api/integrations/hubspot/webhook"
    sig = base64.b64encode(hmac.new(b"secret", b"POST" + uri.encode() + body + ts.encode(), hashlib.sha256).digest()).decode()
    assert verify_hubspot_signature("secret", "POST", uri, body, ts, sig)
    assert not verify_hubspot_signature("secret", "POST", uri, body + b" ", ts, sig)
    assert not verify_hubspot_signature("secret", "POST", uri, body, str(int(ts) - 10 * 60 * 1000), sig)
