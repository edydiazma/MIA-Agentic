"""Rastreo por anuncio (§16): publicación del referral, cruce publicación → anuncio, creativo, fuente del cliente,
inversión de Meta y Google Ads y reporte por anuncio. HTTP simulado."""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app import ad_enrichment, conversions
from app.ads import spend
from app.attribution import parse_source_url, referral_values
from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    AdEntity,
    AdSpendDaily,
    Agent,
    Attribution,
    AttributionTouch,
    Channel,
    Contact,
    Conversation,
    IntegrationConnection,
    Organization,
    Typification,
    utcnow,
)
from app.secrets_vault import put_secret
from tests.conftest import settle, text, unique_phone

USER_URL = ("https://www.facebook.com/story.php?story_fbid=1060896133661462&id=100092232559076"
            "&post_id=100092232559076_1060896133661462")
POST_ID = "100092232559076_1060896133661462"
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
    monkeypatch.setattr(s, "meta_capi_token", "METATOKEN")
    monkeypatch.setattr(s, "google_ads_developer_token", "DEVTOKEN")
    calls.clear()
    routes.clear()


async def _connect(provider: str, org: int = 1, **fields) -> None:
    async with SessionLocal() as s:
        conn = await s.scalar(select(IntegrationConnection).where(IntegrationConnection.organization_id == org,
                                                                  IntegrationConnection.provider == provider))
        if not conn:
            conn = IntegrationConnection(organization_id=org, provider=provider)
            s.add(conn)
            await s.flush()
        conn.status, conn.last_error = "connected", None
        for k, v in fields.items():
            setattr(conn, k, v)
        if provider == "google_ads" and not conn.access_token_secret_id:
            conn.access_token_secret_id = await put_secret(s, "ya29.ads", f"google_ads_access:{conn.id}")
        conn.expires_at = utcnow() + timedelta(hours=1)
        await s.commit()


async def _cleanup(org: int = 1) -> None:
    async with SessionLocal() as s:
        await s.execute(delete(IntegrationConnection).where(IntegrationConnection.organization_id == org,
                                                            IntegrationConnection.provider.in_(["meta", "google_ads"])))
        await s.execute(delete(AdEntity).where(AdEntity.organization_id == org))
        await s.execute(delete(AdSpendDaily).where(AdSpendDaily.organization_id == org))
        await s.commit()


async def _attr(phone: str) -> Attribution:
    async with SessionLocal() as s:
        return (await s.scalars(select(Attribution).join(Conversation, Conversation.id == Attribution.conversation_id)
                                .where(Conversation.contact.has(wa_id=phone))
                                .order_by(Attribution.id.desc()).limit(1))).first()


async def _contact(phone: str) -> Contact:
    async with SessionLocal() as s:
        return await s.scalar(select(Contact).where(Contact.organization_id == 1, Contact.wa_id == phone))


def _meta_ad(ad_id: str, story: str | None, campaign: str = "Lanzamiento Isuzu", name: str = "Isuzu Estacas - Video"):
    return {"id": ad_id, "name": name, "effective_status": "ACTIVE", "account_id": "111",
            "adset": {"id": "AS1", "name": "Bogotá 25-45", "destination_type": "WHATSAPP"},
            "campaign": {"id": "C1", "name": campaign},
            "creative": {"effective_object_story_id": story, "title": "Isuzu Estacas", "body": "Precio de lanzamiento",
                         "image_url": "https://cdn.example/img.jpg", "thumbnail_url": "https://cdn.example/t.jpg"}}


def test_parse_source_url_variants():
    assert parse_source_url(USER_URL) == {"page_id": "100092232559076", "post_id": POST_ID}
    assert parse_source_url("https://www.facebook.com/story.php?story_fbid=1060896133661462&id=100092232559076") == \
        {"page_id": "100092232559076", "post_id": POST_ID}
    assert parse_source_url("https://m.facebook.com/permalink.php?story_fbid=1060896133661462&id=100092232559076") \
        ["post_id"] == POST_ID
    assert parse_source_url("https://www.facebook.com/100092232559076/posts/1060896133661462/")["post_id"] == POST_ID
    assert parse_source_url("https://www.facebook.com/miPagina/posts/1060896133661462") == \
        {"page_id": None, "post_id": None}  # alias de página: sin id numérico no se arma el story id
    assert parse_source_url("https://www.instagram.com/p/C9xYz_1/")["post_id"] == "ig:C9xYz_1"
    assert parse_source_url("https://fb.me/2abc") == {"page_id": None, "post_id": None}
    assert parse_source_url(None) == {"page_id": None, "post_id": None}
    v = referral_values({"source_type": "post", "source_id": "1060896133661462", "source_url": None})
    assert v["post_id"] == "1060896133661462" and v["ad_id"] is None  # sin página: el id tal cual
    v = referral_values({"source_type": "ad", "source_id": "AD9", "headline": "H", "video_url": "https://v.mp4",
                         "media_type": "video", "thumbnail_url": "https://t.jpg"})
    assert (v["ad_id"], v["ad_headline"], v["ad_media_url"], v["ad_thumbnail_url"]) == \
        ("AD9", "H", "https://v.mp4", "https://t.jpg")


async def test_post_referral_resolves_to_specific_ad(client):
    c = client
    await _connect("meta", settings={"ad_account_id": "act_111"})
    try:
        phone = unique_phone("57317")
        await c.post("/webhooks/whatsapp", json=text(phone, f"ads.{phone}.1", "Hola, quiero la Isuzu Estacas", referral={
            "source_type": "post", "source_id": "1060896133661462", "source_url": USER_URL,
            "headline": "Isuzu Estacas", "body": "Precio de lanzamiento", "media_type": "image",
            "image_url": "https://cdn.example/img.jpg", "ctwa_clid": "CLID-POST"}))
        await settle()
        a = await _attr(phone)
        assert (a.channel, a.source_type, a.post_id, a.page_id, a.ad_id) == \
            ("meta_ctwa", "post", POST_ID, "100092232559076", None)
        assert a.ad_headline == "Isuzu Estacas" and a.ad_media_url == "https://cdn.example/img.jpg"
        assert a.enrichment_status == "pending"
        assert (await _contact(phone)).first_source_label == "Meta · Publicación"

        # El filtro de la API trae el anuncio que promocionó la publicación
        routes["act_111/ads"] = httpx.Response(200, json={"data": [_meta_ad("AD-POST-9", POST_ID)]})
        assert await ad_enrichment.enrich_id(a.id) == "done"
        a = await _attr(phone)
        assert (a.ad_id, a.ad_name, a.platform_campaign_name, a.ad_group_name) == \
            ("AD-POST-9", "Isuzu Estacas - Video", "Lanzamiento Isuzu", "Bogotá 25-45")
        k = await _contact(phone)
        assert k.first_source_ad_id == "AD-POST-9"
        assert k.first_source_label == "Meta · Lanzamiento Isuzu · Isuzu Estacas - Video"
        async with SessionLocal() as s:
            touch = await s.scalar(select(AttributionTouch).where(AttributionTouch.conversation_id == a.conversation_id))
            assert (touch.post_id, touch.ad_id, touch.campaign_name) == (POST_ID, "AD-POST-9", "Lanzamiento Isuzu")
            ent = await s.scalar(select(AdEntity).where(AdEntity.organization_id == 1,
                                                        AdEntity.external_id == "AD-POST-9"))
            assert (ent.effective_object_story_id, ent.status, ent.destination, ent.headline) == \
                (POST_ID, "ACTIVE", "whatsapp", "Isuzu Estacas")

        # Otro cliente desde la misma publicación: cruce local, sin llamar a Meta
        calls.clear()
        phone2 = unique_phone("57317")
        await c.post("/webhooks/whatsapp", json=text(phone2, f"ads.{phone2}.2", "Hola, info Isuzu", referral={
            "source_type": "post", "source_id": "1060896133661462", "source_url": USER_URL}))
        await settle()
        a2 = await _attr(phone2)
        assert await ad_enrichment.enrich_id(a2.id) == "done"
        assert (await _attr(phone2)).ad_id == "AD-POST-9" and not calls

        # Publicación orgánica (ningún anuncio la promocionó): queda "Publicación"
        routes["act_111/ads"] = httpx.Response(200, json={"data": []})
        phone3 = unique_phone("57317")
        await c.post("/webhooks/whatsapp", json=text(phone3, f"ads.{phone3}.3", "Hola", referral={
            "source_type": "post", "source_id": "999", "source_url":
                "https://www.facebook.com/story.php?story_fbid=999&id=100092232559076"}))
        await settle()
        a3 = await _attr(phone3)
        assert await ad_enrichment.enrich_id(a3.id) == "done"
        assert (await _attr(phone3)).ad_id is None
        assert (await _contact(phone3)).first_source_label == "Meta · Publicación"
    finally:
        await _cleanup()


async def test_ad_creative_and_first_vs_last_source(client):
    c = client
    await _connect("meta", settings={"ad_account_id": "111"})
    try:
        phone = unique_phone("57317")
        await c.post("/webhooks/whatsapp", json=text(phone, f"ads.{phone}.10", "Hola, me interesa", referral={
            "source_type": "ad", "source_id": "AD-CR-1", "headline": "Promo CX-5", "ctwa_clid": "C1"}))
        await settle()
        routes["/AD-CR-1"] = httpx.Response(200, json=_meta_ad("AD-CR-1", "PAGE_1", "Mazda Q4", "CX-5 carrusel"))
        a = await _attr(phone)
        assert await ad_enrichment.enrich_id(a.id) == "done"
        a = await _attr(phone)
        assert (a.ad_name, a.post_id, a.ad_body, a.ad_thumbnail_url) == \
            ("CX-5 carrusel", "PAGE_1", "Precio de lanzamiento", "https://cdn.example/t.jpg")
        assert a.ad_headline == "Promo CX-5"  # el del referral manda sobre el del creativo

        # El mismo cliente vuelve por otro anuncio: el primer toque se conserva, el último cambia
        await c.post("/webhooks/whatsapp", json=text(phone, f"ads.{phone}.11", "Hola otra vez", referral={
            "source_type": "ad", "source_id": "AD-CR-2", "ctwa_clid": "C2"}))
        await settle()
        k = await _contact(phone)
        assert (k.first_source_ad_id, k.last_source_ad_id) == ("AD-CR-1", "AD-CR-2")
        assert k.first_source_label == "Meta · Mazda Q4 · CX-5 carrusel"
    finally:
        await _cleanup()


async def test_spend_sync_meta_and_google_idempotent(client, monkeypatch):
    await _connect("meta", settings={"ad_account_id": "act_111"})
    await _connect("google_ads", external_account_id="123-456-7890")
    today = date.today()

    def insights(spend_value: str) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"ad_id": "AD-S-1", "ad_name": "Video", "adset_id": "AS1", "adset_name": "Set", "campaign_id": "C1",
             "campaign_name": "Camp", "spend": spend_value, "impressions": "1000", "clicks": "40",
             "account_currency": "COP", "date_start": today.isoformat(),
             "actions": [{"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "6"},
                         {"action_type": "link_click", "value": "40"}]}]})

    async def fake_google(session, conn, query):
        row = {"segments": {"date": today.isoformat()}, "campaign": {"id": "987", "name": "Búsqueda"},
               "metrics": {"costMicros": "25500000", "impressions": "500", "clicks": "20"},
               "customer": {"currencyCode": "COP"}}
        if "FROM ad_group_ad" in query:
            return [{**row, "adGroup": {"id": "55", "name": "Grupo"}, "adGroupAd": {"ad": {"id": "777", "name": "RSA"}}}]
        return [row, {**row, "campaign": {"id": "PMAX", "name": "Performance Max"}}]

    monkeypatch.setattr(spend, "google_search", fake_google)
    try:
        routes["act_111/insights"] = insights("12.50")
        out = await spend.sync_org(1)
        assert out == {"meta": 1, "google_ads": 3}
        meta_call = next(p for m, u, p in calls if "act_111/insights" in u)
        assert meta_call["params"]["level"] == "ad" and meta_call["params"]["time_increment"] == 1
        routes["act_111/insights"] = insights("20.00")  # la plataforma ajusta la cifra: se actualiza, no se duplica
        await spend.sync_org(1)
        async with SessionLocal() as s:
            rows = (await s.scalars(select(AdSpendDaily).where(AdSpendDaily.organization_id == 1)
                                    .order_by(AdSpendDaily.platform, AdSpendDaily.level, AdSpendDaily.entity_id))).all()
        got = {(r.platform, r.level, r.entity_id): r for r in rows}
        assert len(rows) == 4
        m = got[("meta", "ad", "AD-S-1")]
        assert (float(m.spend), m.impressions, m.platform_conversations, m.currency) == (20.0, 1000, 6, "COP")
        assert float(got[("google_ads", "ad", "777")].spend) == 25.5
        assert ("google_ads", "campaign", "PMAX") in got

        # Error de Meta: queda en la conexión y no detiene a Google
        routes["act_111/insights"] = httpx.Response(400, json={"error": {"message": "(#200) ads_read"}})
        out = await spend.sync_org(1)
        assert str(out["meta"]).startswith("error") and out["google_ads"] == 3
        async with SessionLocal() as s:
            conn = await s.scalar(select(IntegrationConnection).where(
                IntegrationConnection.organization_id == 1, IntegrationConnection.provider == "meta"))
            assert "ads_read" in (conn.last_error or "")
    finally:
        await _cleanup()


async def test_ads_report_math_and_isolation(client):
    org = 9351  # id propio: 9301 es de test_security
    async with SessionLocal() as s:
        if not await s.get(Organization, org):
            s.add(Organization(id=org, name="Org anuncios", slug="org-anuncios", timezone="America/Bogota"))
            await s.flush()
            s.add(Agent(organization_id=org, email="ads9351@test.com", name="Ads", role="admin",
                        password_hash=hash_password("secret9351")))
            ch = Channel(organization_id=org, name="WA ads", phone_number_id="PN-ADS-9351")
            venta = Typification(organization_id=org, name="Venta", is_success=True, position=1)
            s.add_all([ch, venta])
            await s.flush()
            k = Contact(organization_id=org, wa_id="573179351001", name="Cliente anuncio")
            s.add(k)
            await s.flush()
            now = utcnow()
            conv = Conversation(organization_id=org, contact_id=k.id, channel_id=ch.id, status="closed",
                                closed_at=now, typification_id=venta.id, created_at=now, last_message_at=now)
            s.add(conv)
            await s.flush()
            s.add(Attribution(organization_id=org, conversation_id=conv.id, contact_id=k.id, channel="meta_ctwa",
                              matched_by="ctwa_referral", ad_id="AD-R-1", ad_name="Anuncio uno",
                              platform_campaign_name="Campaña R", touches=1))
            s.add_all([
                AdSpendDaily(organization_id=org, platform="meta", level="ad", entity_id="AD-R-1", day=date.today(),
                             ad_id="AD-R-1", ad_name="Anuncio uno", campaign_name="Campaña R", spend=100,
                             impressions=2000, clicks=50, currency="COP"),
                AdSpendDaily(organization_id=org, platform="meta", level="ad", entity_id="AD-R-2", day=date.today(),
                             ad_id="AD-R-2", ad_name="Anuncio dos", campaign_name="Campaña R", spend=50,
                             impressions=0, clicks=0, currency="COP")])
            s.add(AdEntity(organization_id=org, platform="meta", entity_type="ad", external_id="AD-R-1",
                           name="Anuncio uno", thumbnail_url="https://t/1.jpg", headline="Titular", status="ACTIVE"))
            await s.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c2:
        tok = (await c2.post("/api/auth/login", json={"email": "ads9351@test.com", "password": "secret9351"})).json()
        c2.headers["Authorization"] = f"Bearer {tok['access_token']}"
        r = await c2.get("/api/reports/ads")
        assert r.status_code == 200, r.text
        data = r.json()
        t = data["totals"]
        assert (t["spend"], t["conversations"], t["new_contacts"], t["sales"], t["currency"]) == (150.0, 1, 1, 1, "COP")
        assert t["cost_per_conversation"] == 150.0 and t["cost_per_sale"] == 150.0 and t["roas"] == 0.0
        ads = {a["ad_key"]: a for a in data["ads"]}
        a1, a2 = ads["AD-R-1"], ads["AD-R-2"]
        assert (a1["spend"], a1["conversations"], a1["cost_per_conversation"], a1["ctr"]) == (100.0, 1, 100.0, 2.5)
        assert (a1["thumbnail_url"], a1["headline"], a1["status"]) == ("https://t/1.jpg", "Titular", "ACTIVE")
        assert a2["conversations"] == 0 and a2["cost_per_conversation"] is None and a2["ctr"] is None
        assert data["campaigns"][0]["campaign_name"] == "Campaña R" and data["campaigns"][0]["spend"] == 150.0
        assert sum(x["spend"] for x in data["series"]) == 150.0

        d = (await c2.get("/api/ads/meta/AD-R-1")).json()
        assert d["creative"]["headline"] == "Titular" and d["contacts"][0]["name"] == "Cliente anuncio"
        assert d["totals"]["spend"] == 100.0 and d["series"][0]["spend"] == 100.0
        assert (await c2.get("/api/ads/meta/NO-EXISTE")).status_code == 404
        assert (await c2.get("/api/ads/tiktok/AD-R-1")).status_code == 404

    # Aislamiento: la empresa 1 no ve los anuncios de la 9351
    r = await client.get("/api/reports/ads")
    assert "AD-R-1" not in {a["ad_key"] for a in r.json()["ads"]}
    assert (await client.get("/api/ads/meta/AD-R-1")).status_code == 404
