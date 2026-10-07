"""Atribución de WhatsApp en el CRM: propiedades de HubSpot, mapeo en Salesforce, push al llegar por un mensaje
disparador y nunca de vuelta (solo push)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.crm import connections as cx
from app.crm import salesforce
from app.db import SessionLocal
from app.models import Agent, Attribution, IntegrationMapping, IntegrationOutbox
from tests.conftest import settle, text
from tests.test_crm import FakeHubSpot, fake_hubspot  # noqa: F401, F811  (fixture)

PHONE = "573118880001"


def _with_properties(fake: FakeHubSpot) -> list[dict]:
    """Agrega al HubSpot falso los endpoints de propiedades (409 si ya existe)."""
    created: list[dict] = []
    names: set[str] = set()
    original = fake.send

    async def send(method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        path = urlparse(url).path
        if path in ("/crm/v3/properties/contacts/groups", "/crm/v3/properties/contacts") and method == "POST":
            fake.calls.append((method, path))
            if json_body["name"] in names:
                return httpx.Response(409, json={"message": "Property already exists"})
            names.add(json_body["name"])
            created.append(json_body)
            return httpx.Response(201, json=json_body)
        return await original(method, url, headers=headers, json_body=json_body, data=data, params=params,
                              timeout=timeout)

    fake.send = send
    return created


async def _org(c) -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(Agent.organization_id).where(Agent.email == "admin@test.com"))


async def test_hubspot_attribution_properties_push_only(client, fake_hubspot, monkeypatch):  # noqa: F811
    c = client
    created = _with_properties(fake_hubspot)
    from app.crm import hubspot

    monkeypatch.setattr(hubspot, "send", fake_hubspot.send)
    assert (await c.post("/api/integrations/hubspot/token", json={"token": "pat-attr"})).json()["connected"]

    r = await c.post("/api/integrations/hubspot/attribution-properties")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 15 and body["existing"] == [] and body["mapped"] == 15
    group = next(p for p in created if p["name"] == "whatsapp_attribution")
    assert group["label"] == "Atribución WhatsApp"
    touches = next(p for p in created if p["name"] == "wa_touches")
    assert touches["type"] == "number" and touches["groupName"] == "whatsapp_attribution"
    again = (await c.post("/api/integrations/hubspot/attribution-properties")).json()
    assert again["created"] == [] and len(again["existing"]) == 15 and again["mapped"] == 15

    maps = (await c.get("/api/integrations/hubspot/mappings")).json()
    attr_maps = [m for m in maps["mappings"] if m["local_field"].startswith("attribution.")]
    assert len(attr_maps) == 15 and {m["direction"] for m in attr_maps} == {"push"}
    assert "attribution.utm_campaign" in maps["local_fields"]["contact"]
    assert "attribution.campaign_name" in maps["local_fields"]["deal"]
    assert maps["field_groups"]["attribution"]["push_only"] is True

    # Guardar el mapeo con "both" no lo vuelve bidireccional
    payload = [{k: m[k] for k in ("object", "local_field", "remote_property", "direction", "transform")}
               for m in maps["mappings"]]
    for m in payload:
        if m["local_field"] == "attribution.utm_source":
            m["direction"] = "both"
    assert (await c.put("/api/integrations/hubspot/mappings", json={"mappings": payload})).status_code == 200
    async with SessionLocal() as s:
        d = await s.scalar(select(IntegrationMapping.direction).where(
            IntegrationMapping.local_field == "attribution.utm_source"))
    assert d == "push"

    # Llega un cliente por un mensaje disparador → se encola el contacto y el push lleva la atribución
    link = (await c.post("/api/wa-links", json={
        "name": "Feria Bogotá QR", "platform": "qr", "trigger_text": "Hola, los vi en la feria de Bogotá",
        "utm_campaign": "feria_bogota_2026"})).json()
    await c.post("/webhooks/whatsapp", json=text(PHONE, "crmattr.1", "Hola, los vi en la feria de Bogotá"))
    await settle()
    async with SessionLocal() as s:
        attr = (await s.scalars(select(Attribution).where(Attribution.link_id == link["id"]))).first()
        assert attr and attr.channel == "offline"
        queued = await s.scalar(select(IntegrationOutbox.id).where(
            IntegrationOutbox.entity_type == "contact", IntegrationOutbox.entity_id == attr.contact_id,
            IntegrationOutbox.status == "pending"))
    assert queued, "el toque nuevo encola el contacto en el CRM"

    r = await c.post("/api/integrations/hubspot/sync")
    assert r.status_code == 200, r.text
    rid, hs = next((k, p) for k, p in fake_hubspot.contacts.items() if p.get("phone") == f"+{PHONE}")
    assert hs["wa_utm_campaign"] == "feria_bogota_2026" and hs["wa_trigger_link"] == "Feria Bogotá QR"
    assert hs["wa_channel"] == "Offline (QR, SMS)" and hs["wa_utm_source"] == "qr"
    assert hs["wa_first_touch_channel"] == "offline" and hs["wa_touches"] == "1"

    # Un cambio en HubSpot sobre una propiedad de atribución no se aplica localmente
    fake_hubspot.contacts[rid].update({"wa_utm_campaign": "editado_en_hubspot",
                                       "lastmodifieddate": FakeHubSpot._stamp(datetime.now(UTC) + timedelta(seconds=5))})
    await c.post("/api/integrations/hubspot/sync")
    async with SessionLocal() as s:
        assert (await s.get(Attribution, attr.id)).utm_campaign == "feria_bogota_2026"
    await c.delete("/api/integrations/hubspot")


async def test_salesforce_attribution_mapping_from_describe(client, monkeypatch):
    c = client
    calls = []

    async def fake_send(method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        calls.append((method, urlparse(url).path))
        if urlparse(url).path.endswith("/sobjects/Contact/describe"):
            return httpx.Response(200, json={"fields": [
                {"name": "LeadSource", "updateable": True, "restrictedPicklist": False},
                {"name": "WA_UTM_Source__c", "updateable": True},
                {"name": "WA_UTM_Campaign__c", "updateable": True},
                {"name": "WA_GCLID__c", "updateable": False},
            ]})
        return httpx.Response(404, json=[{"message": "no fake"}])

    monkeypatch.setattr(salesforce, "send", fake_send)
    org = await _org(c)
    async with SessionLocal() as s:
        await cx.upsert_connection(s, org, "salesforce", None, {"access_token": "sf-at"}, "00Dxx",
                                   instance_url="https://sf.test")
    r = await c.post("/api/integrations/salesforce/attribution-mapping")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "Contact"
    assert body["mapped"] == ["LeadSource", "WA_UTM_Source__c", "WA_UTM_Campaign__c"]
    assert "WA_GCLID__c" in body["missing"] and "WA_Channel__c" in body["missing"] and body["skipped"] == []
    assert calls == [("GET", "/services/data/v61.0/sobjects/Contact/describe")]
    maps = (await c.get("/api/integrations/salesforce/mappings")).json()["mappings"]
    by_remote = {m["remote_property"]: m for m in maps}
    assert by_remote["LeadSource"]["local_field"] == "attribution.channel_label"
    assert by_remote["WA_UTM_Campaign__c"]["direction"] == "push"
    async with SessionLocal() as s:
        conn = await cx.get_connection(s, org, "salesforce")
        await cx.disconnect(s, conn)
