"""Enriquecimiento de atribuciones (Meta: anuncio → campaña/conjunto; Google Ads: gclid → click_view) y
selectores de anuncios para los mensajes disparadores. HTTP simulado."""

from datetime import UTC, timedelta

import httpx
import pytest
from sqlalchemy import delete, select, update

from app import ad_enrichment, conversions
from app.config import get_settings
from app.db import SessionLocal
from app.models import AdEntity, Attribution, Conversation, IntegrationConnection, utcnow
from app.secrets_vault import put_secret
from tests.conftest import settle, text

calls: list[tuple[str, str, dict]] = []
routes: dict[str, httpx.Response] = {}


class FakeHTTP:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _answer(self, method, url, payload):
        calls.append((method, url, payload))
        for key, resp in routes.items():
            if key in url:
                return resp
        return httpx.Response(404, json={"error": "sin ruta"})

    async def get(self, url, params=None, headers=None):
        return self._answer("GET", url, {"params": params, "headers": headers})

    async def post(self, url, json=None, data=None, headers=None):
        return self._answer("POST", url, {"json": json, "data": data, "headers": headers})


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    monkeypatch.setattr(conversions, "http_client", lambda timeout=30: FakeHTTP())
    monkeypatch.setattr(ad_enrichment, "enrich_soon", lambda attribution_id: None)  # se llama explícitamente
    s = get_settings()
    monkeypatch.setattr(s, "google_ads_developer_token", "DEVTOKEN")
    monkeypatch.setattr(s, "meta_capi_token", "METATOKEN")
    calls.clear()
    routes.clear()


async def _connect(provider: str, status: str = "connected", **fields) -> None:
    async with SessionLocal() as s:
        conn = await s.scalar(select(IntegrationConnection).where(IntegrationConnection.organization_id == 1,
                                                                  IntegrationConnection.provider == provider))
        if not conn:
            conn = IntegrationConnection(organization_id=1, provider=provider)
            s.add(conn)
            await s.flush()
        conn.status = status
        for k, v in fields.items():
            setattr(conn, k, v)
        if provider == "google_ads" and not conn.access_token_secret_id:
            conn.access_token_secret_id = await put_secret(s, "ya29.enrich", f"google_ads_access:{conn.id}")
        conn.expires_at = utcnow() + timedelta(hours=1)
        await s.commit()


async def _cleanup() -> None:
    async with SessionLocal() as s:
        await s.execute(delete(IntegrationConnection).where(IntegrationConnection.organization_id == 1,
                                                            IntegrationConnection.provider.in_(["meta", "google_ads"])))
        await s.execute(delete(AdEntity).where(AdEntity.organization_id == 1))
        # Las conversaciones de prueba no deben pesar en el reporte de Click to WA de otras pruebas
        await s.execute(update(Conversation).where(Conversation.ad_source_id == "AD-ENR-1")
                        .values(ad_source_type=None, ad_source_id=None, ad_headline=None, ad_ctwa_clid=None))
        await s.commit()


async def _attr_of(phone: str) -> Attribution:
    async with SessionLocal() as s:
        return (await s.scalars(select(Attribution).join(Conversation, Conversation.id == Attribution.conversation_id)
                                .where(Conversation.contact.has(wa_id=phone)))).first()


async def test_meta_and_google_enrichment(client):
    c = client
    try:
        await _connect("meta", settings={"ad_account_id": "555"})
        await _connect("google_ads", external_account_id="1112223333", settings={"login_customer_id": "999-000-1111"})

        # Meta: dos conversaciones del mismo anuncio → una sola consulta (caché)
        routes["/AD-ENR-1"] = httpx.Response(200, json={
            "id": "AD-ENR-1", "name": "Anuncio CX-5 video", "adset": {"id": "AS1", "name": "Bogotá 25-45"},
            "campaign": {"id": "C1", "name": "Promo CX-5 octubre"}})
        for i, phone in enumerate(["573160000001", "573160000002"]):
            await c.post("/webhooks/whatsapp", json=text(phone, f"enr.{i}", "Hola, me interesa", referral={
                "source_type": "ad", "source_id": "AD-ENR-1", "ctwa_clid": f"CL-{i}"}))
            await settle()
        a1, a2 = await _attr_of("573160000001"), await _attr_of("573160000002")
        assert a1.enrichment_status == a2.enrichment_status == "pending"
        assert await ad_enrichment.enrich_id(a1.id) == "done"
        assert await ad_enrichment.enrich_id(a2.id) == "done"
        meta_calls = [u for m, u, _p in calls if "/AD-ENR-1" in u]
        assert len(meta_calls) == 1
        get_call = next(p for m, u, p in calls if "/AD-ENR-1" in u)
        assert get_call["params"]["access_token"] == "METATOKEN" and "campaign{id,name}" in get_call["params"]["fields"]
        a2 = await _attr_of("573160000002")
        assert (a2.platform_campaign_name, a2.ad_group_name, a2.ad_name, a2.enrichment_status) == (
            "Promo CX-5 octubre", "Bogotá 25-45", "Anuncio CX-5 video", "done")
        assert a2.platform_campaign_id == "C1" and a2.enriched_at

        # Google Ads: gclid → click_view del día del clic
        phone = "573160000003"
        await c.post("/webhooks/whatsapp", json=text(phone, "enr.g", "Buenas tardes"))
        await settle()
        a3 = await _attr_of(phone)
        async with SessionLocal() as s:
            await s.execute(update(Attribution).where(Attribution.id == a3.id)
                            .values(gclid="GCL-ENR", enrichment_status="pending"))
            await s.commit()
        routes["googleAds:search"] = httpx.Response(200, json={"results": [{
            "clickView": {"gclid": "GCL-ENR", "keywordInfo": {"text": "mazda cx5 precio"}},
            "campaign": {"id": "777", "name": "Search - SUV"}, "adGroup": {"id": "888", "name": "CX-5"}}]})
        assert await ad_enrichment.enrich_id(a3.id) == "done"
        method, url, payload = next(x for x in calls if "googleAds:search" in x[1])
        assert url.endswith("/customers/1112223333/googleAds:search")
        assert payload["headers"]["login-customer-id"] == "9990001111"
        assert payload["headers"]["developer-token"] == "DEVTOKEN"
        query = payload["json"]["query"]
        assert "FROM click_view" in query and "click_view.gclid = 'GCL-ENR'" in query
        assert f"segments.date = '{a3.created_at.astimezone(UTC).date().isoformat()}'" in query
        a3 = await _attr_of(phone)
        assert (a3.platform_campaign_name, a3.ad_group_name, a3.keyword) == ("Search - SUV", "CX-5", "mazda cx5 precio")

        # Fallo del proveedor → failed (sin excepción); la caché no se ensucia
        phone = "573160000004"
        await c.post("/webhooks/whatsapp", json=text(phone, "enr.f", "Hola"))
        await settle()
        a4 = await _attr_of(phone)
        async with SessionLocal() as s:
            await s.execute(update(Attribution).where(Attribution.id == a4.id)
                            .values(gclid="GCL-BAD", enrichment_status="pending"))
            await s.commit()
        routes["googleAds:search"] = httpx.Response(400, json={"error": {"message": "invalid"}})
        assert await ad_enrichment.enrich_id(a4.id) == "failed"
        a4 = await _attr_of(phone)
        assert a4.enrichment_status == "failed" and a4.platform_campaign_name is None
        async with SessionLocal() as s:
            assert not await s.scalar(select(AdEntity.id).where(AdEntity.external_id == "GCL-BAD"))

        # Atribución sin anuncio ni gclid: nada que hacer
        assert await ad_enrichment.enrich_id(a1.id) == "skipped"  # ya está done
    finally:
        await _cleanup()


async def test_ad_pickers(client):
    c = client
    try:
        await _cleanup()
        r = await c.get("/api/wa-links/meta-ads")
        assert r.status_code == 409 and "Conecta Meta" in r.json()["detail"]
        assert (await c.get("/api/wa-links/google-campaigns")).status_code == 409

        await _connect("meta")
        r = await c.get("/api/wa-links/meta-ads")
        assert r.status_code == 409 and "cuenta publicitaria" in r.json()["detail"]
        # Se configura desde la conexión de Meta (act_ opcional)
        r = await c.put("/api/attribution/connections/meta", json={"ad_account_id": "act_555"})
        assert r.status_code == 200 and r.json()["settings"]["ad_account_id"] == "555"

        routes["/act_555/ads"] = httpx.Response(200, json={"data": [
            {"id": "A1", "name": "CX-5 video", "effective_status": "ACTIVE",
             "campaign": {"id": "C1", "name": "Promo CX-5"}, "adset": {"name": "Bogotá"}},
            {"id": "A2", "name": "Mazda 3 carrusel", "effective_status": "PAUSED",
             "campaign": {"id": "C2", "name": "Mazda 3"}, "adset": {"name": "Medellín"}}]})
        r = await c.get("/api/wa-links/meta-ads")
        assert r.status_code == 200
        assert r.json()[0] == {"id": "A1", "name": "CX-5 video", "status": "ACTIVE", "campaign_id": "C1",
                               "campaign_name": "Promo CX-5", "adset_name": "Bogotá"}
        r = await c.get("/api/wa-links/meta-ads", params={"q": "medellín"})
        assert [a["id"] for a in r.json()] == ["A2"]
        routes["/act_555/ads"] = httpx.Response(500, json={"error": "boom"})
        assert (await c.get("/api/wa-links/meta-ads")).status_code == 502

        await _connect("google_ads", external_account_id="1112223333")
        routes["googleAds:search"] = httpx.Response(200, json={"results": [
            {"campaign": {"id": "777", "name": "Search - SUV", "status": "ENABLED"}}]})
        r = await c.get("/api/wa-links/google-campaigns")
        assert r.status_code == 200 and r.json() == [{"id": "777", "name": "Search - SUV", "status": "ENABLED"}]
        query = next(p for m, u, p in calls if "googleAds:search" in u)["json"]["query"]
        assert "FROM campaign" in query and "REMOVED" in query

        # Las rutas fijas no las captura /{link_id}
        assert (await c.get("/api/wa-links/999999")).status_code == 404
    finally:
        await _cleanup()


async def test_meta_capi_website_event_source_url(client):
    """Eventos de sitio web (fbc) llevan event_source_url desde la atribución."""
    from app.models import ConversionAction, ConversionEvent, ConversionUpload

    async with SessionLocal() as s:
        action = ConversionAction(organization_id=1, name="Web CAPI", trigger="deal_won",
                                  meta={"dataset_id": "DSW", "event_name": "Lead"})
        s.add(action)
        await s.flush()
        conv_id = await s.scalar(select(Conversation.id).where(Conversation.organization_id == 1).limit(1))
        contact_id = await s.scalar(select(Conversation.contact_id).where(Conversation.id == conv_id))
        ev = ConversionEvent(organization_id=1, action_id=action.id, conversation_id=conv_id, contact_id=contact_id,
                             currency="COP", dedupe_key=f"t-web-{conv_id}", occurred_at=utcnow(),
                             attribution={"fbc": "fb.1.1.abc", "landing_url": "https://tienda.com/cx5"})
        s.add(ev)
        await s.flush()
        up = ConversionUpload(event_id=ev.id, destination="meta_capi", status="pending")
        s.add(up)
        await s.commit()
        up_id = up.id
    routes["/DSW/events"] = httpx.Response(200, json={"events_received": 1})
    async with SessionLocal() as s:
        up = await s.get(ConversionUpload, up_id)
        await conversions.process_upload(s, up)
    sent = next(p for m, u, p in calls if "/DSW/events" in u)["json"]["data"][0]
    assert sent["action_source"] == "website" and sent["event_source_url"] == "https://tienda.com/cx5"
    async with SessionLocal() as s:
        await s.execute(delete(ConversionUpload).where(ConversionUpload.id == up_id))
        await s.execute(delete(ConversionEvent).where(ConversionEvent.dedupe_key == f"t-web-{conv_id}"))
        await s.execute(delete(ConversionAction).where(ConversionAction.name == "Web CAPI"))
        await s.commit()
