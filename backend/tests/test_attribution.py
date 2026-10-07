"""Atribución web / Click to WA y conversiones hacia Google Ads y Meta CAPI (HTTP simulado)."""

from datetime import timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select, update

from app import conversions
from app.config import get_settings
from app.db import SessionLocal
from app.models import Alert, Channel, ConversionEvent, ConversionUpload, IntegrationConnection, utcnow
from app.secrets_vault import put_secret
from tests.conftest import settle, text

ORIGIN = {"origin": "https://www.tienda.com"}
sent: list[tuple[str, dict]] = []
responses: dict[str, httpx.Response] = {}


class FakeHTTP:
    """Simula Google Ads y Meta según la URL; las respuestas se configuran por prueba."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, data=None, headers=None):
        sent.append((url, {"json": json, "data": data, "headers": headers}))
        for key, resp in responses.items():
            if key in url:
                return resp
        return httpx.Response(200, json={})


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    monkeypatch.setattr(conversions, "http_client", lambda timeout=30: FakeHTTP())
    s = get_settings()
    monkeypatch.setattr(s, "google_ads_developer_token", "DEVTOKEN")
    monkeypatch.setattr(s, "meta_capi_token", "METATOKEN")
    sent.clear()
    responses.clear()


async def _site(c) -> dict:
    sites = (await c.get("/api/tracking-sites")).json()
    if sites:
        return sites[0]
    async with SessionLocal() as s:
        await s.execute(update(Channel).values(display_phone="+57 300 111 2222"))
        await s.commit()
    r = await c.post("/api/tracking-sites", json={"name": "Tienda", "allowed_domains": ["https://tienda.com/"],
                                                  "wa_prefill": "Hola, quiero el Onix"})
    assert r.status_code == 200, r.text
    return r.json()


async def _collect(c, key: str, params: dict) -> str:
    r = await c.post("/t/collect", headers={**ORIGIN, "content-type": "text/plain"},
                     content=__import__("json").dumps({"k": key, "vid": "v1", "url": "https://www.tienda.com/onix?x=1",
                                                       "referrer": "https://www.google.com/", "params": params}))
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == "https://www.tienda.com"
    return r.json()["ref_code"]


async def _conv(c, phone):
    return (await c.get("/api/conversations", params={"q": phone})).json()[0]


async def test_tracking_script_collect_and_redirect(client):
    c = client
    site = await _site(c)
    assert site["allowed_domains"] == ["tienda.com"] and site["snippet"].startswith("<script async")
    js = await c.get(f"/t/{site['public_key']}.js")
    assert js.status_code == 200 and site["public_key"] in js.text and "/t/collect" in js.text
    assert len(js.content) < 6000

    code = await _collect(c, site["public_key"], {"gclid": "G-123", "utm_source": "google", "utm_campaign": "onix-oct",
                                                  "utm_term": "onix precio"})
    assert len(code) == 6 and code.isupper()
    # Otro dominio: rechazado
    r = await c.post("/t/collect", headers={"origin": "https://malo.com", "content-type": "text/plain"},
                     content=f'{{"k": "{site["public_key"]}", "vid": "x"}}')
    assert r.status_code == 403

    r = await c.get(f"/t/wa/{site['public_key']}", params={"r": code}, follow_redirects=False)
    assert r.status_code == 302
    loc = unquote(r.headers["location"])
    assert loc.startswith("https://wa.me/573001112222?text=") and f"(ref: {code})" in loc

    traffic = (await c.get("/api/reports/web-traffic")).json()
    assert traffic["totals"]["sessions"] >= 1 and traffic["totals"]["wa_clicks"] >= 1
    assert any(r["campaign"] == "onix-oct" for r in traffic["by_source"])


async def test_attribution_and_conversions(client):
    c = client
    site = await _site(c)
    code = await _collect(c, site["public_key"], {"gclid": "G-777", "utm_source": "google", "utm_medium": "cpc",
                                                  "utm_campaign": "tracker", "utm_term": "tracker 2026"})

    # 1. Web con gclid → google_ads
    phone_g = "573990000001"
    await c.post("/webhooks/whatsapp", json=text(phone_g, "at.1", f"Hola, quiero el Onix (ref: {code.lower()})"))
    await settle()
    conv_g = await _conv(c, phone_g)
    a = (await c.get(f"/api/attributions/conversation/{conv_g['id']}")).json()
    assert a["channel"] == "google_ads" and a["gclid"] == "G-777" and a["utm_term"] == "tracker 2026"

    # 2. Click to WhatsApp de Meta → meta_ctwa
    phone_m = "573990000002"
    await c.post("/webhooks/whatsapp", json=text(phone_m, "at.2", "hola", referral={
        "source_type": "ad", "source_id": "AD9", "headline": "Tracker", "ctwa_clid": "CTWA-1"}))
    await settle()
    conv_m = await _conv(c, phone_m)
    a = (await c.get(f"/api/attributions/conversation/{conv_m['id']}")).json()
    assert a["channel"] == "meta_ctwa" and a["ctwa_clid"] == "CTWA-1" and a["ad_id"] == "AD9"

    # 3. Sin señales → direct
    phone_d = "573990000003"
    await c.post("/webhooks/whatsapp", json=text(phone_d, "at.3", "buenas"))
    await settle()
    a = (await c.get(f"/api/attributions/conversation/{(await _conv(c, phone_d))['id']}")).json()
    assert a["channel"] == "direct"

    # Acción de conversión: tipificación Venta → Google Ads + Meta
    typs = (await c.get("/api/settings/conversations")).json()["typifications"]
    assert "Venta" in typs
    async with SessionLocal() as s:
        from app.models import Typification

        venta = await s.scalar(select(Typification.id).where(Typification.organization_id == 1,
                                                              Typification.name == "Venta"))
    bad = await c.post("/api/conversion-actions", json={"name": "x", "trigger": "typification"})
    assert bad.status_code == 422
    r = await c.post("/api/conversion-actions", json={
        "name": "Venta WhatsApp", "trigger": "typification", "typification_id": venta, "value": 1000000,
        "google_ads": {"customer_id": "123-456-7890", "conversion_action_id": "999"},
        "meta": {"dataset_id": "DS1", "event_name": "Purchase"}})
    assert r.status_code == 200, r.text
    await c.post("/api/conversion-actions/scan")  # inicializa el cursor (no sube historia)

    for conv in (conv_g, conv_m):
        r = await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Venta"})
        assert r.status_code == 200, r.text
    assert (await c.post("/api/conversion-actions/scan")).json()["created"] == 2
    assert (await c.post("/api/conversion-actions/scan")).json()["created"] == 0  # idempotente

    ups = {(u["conversation_id"], u["destination"]): u for u in (await c.get("/api/conversion-uploads")).json()}
    assert ups[(conv_g["id"], "google_ads")]["status"] == "pending"
    assert ups[(conv_g["id"], "meta_capi")]["status"] == "skipped"
    assert ups[(conv_m["id"], "meta_capi")]["status"] == "pending"
    assert ups[(conv_m["id"], "google_ads")]["status"] == "skipped"

    # Conexión de Google Ads con token vigente en Vault
    async with SessionLocal() as s:
        gc = IntegrationConnection(organization_id=1, provider="google_ads", external_account_id="1234567890")
        s.add(gc)
        await s.flush()
        gc.access_token_secret_id = await put_secret(s, "ya29.token", f"google_ads_access:{gc.id}")
        gc.expires_at = utcnow() + timedelta(hours=1)
        await s.commit()

    assert await conversions.upload_due() == 2
    g = next(b for u, b in sent if "uploadClickConversions" in u)
    conv_payload = g["json"]["conversions"][0]
    assert conv_payload["gclid"] == "G-777" and conv_payload["conversionAction"] == "customers/1234567890/conversionActions/999"
    assert conv_payload["conversionValue"] == 1000000.0 and conv_payload["userIdentifiers"][0]["hashedPhoneNumber"]
    assert g["headers"]["developer-token"] == "DEVTOKEN" and g["headers"]["Authorization"] == "Bearer ya29.token"
    m = next(b for u, b in sent if "/DS1/events" in u)
    ev = m["json"]["data"][0]
    assert ev["action_source"] == "business_messaging" and ev["messaging_channel"] == "whatsapp"
    assert ev["user_data"]["ctwa_clid"] == "CTWA-1" and m["json"]["access_token"] == "METATOKEN"
    ups = {(u["conversation_id"], u["destination"]): u for u in (await c.get("/api/conversion-uploads")).json()}
    assert ups[(conv_g["id"], "google_ads")]["status"] == "sent" and ups[(conv_m["id"], "meta_capi")]["status"] == "sent"

    # Reporte de atribución
    rep = (await c.get("/api/reports/attribution")).json()
    channels = {r["channel"]: r for r in rep["by_channel"]}
    assert channels["google_ads"]["sales"] >= 1 and channels["meta_ctwa"]["conversions"] >= 1
    gads = (await c.get("/api/reports/click-to-wa-google")).json()
    assert gads["totals"]["sales"] >= 1 and gads["by_keyword"][0]["keyword"] == "tracker 2026"


async def test_upload_backoff_and_permanent_failure(client):
    c = client
    site = await _site(c)
    code = await _collect(c, site["public_key"], {"gclid": "G-FAIL"})
    phone = "573990000009"
    await c.post("/webhooks/whatsapp", json=text(phone, "bf.1", f"info (ref: {code})"))
    await settle()
    conv = await _conv(c, phone)
    await c.post("/api/conversion-actions/scan")
    await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Venta"})
    await c.post("/api/conversion-actions/scan")

    async with SessionLocal() as s:
        up = (await s.scalars(select(ConversionUpload).join(ConversionEvent).where(
            ConversionEvent.conversation_id == conv["id"], ConversionUpload.destination == "google_ads"))).first()
        responses["uploadClickConversions"] = httpx.Response(503, json={"error": "unavailable"})
        await conversions.process_upload(s, up)
        assert up.status == "pending" and up.attempts == 1
        assert timedelta(seconds=50) < up.next_attempt_at - utcnow() < timedelta(seconds=70)  # 1 min

        responses["uploadClickConversions"] = httpx.Response(200, json={"partialFailureError": {"message": "gclid inválido"}})
        await conversions.process_upload(s, up)
        assert up.status == "failed" and "gclid inválido" in up.error
        assert await s.scalar(select(Alert.id).where(Alert.layer == "conversion", Alert.ref == str(up.event_id)))
