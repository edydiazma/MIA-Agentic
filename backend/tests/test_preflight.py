"""Diagnóstico de integraciones (§20): cada área con HTTP / SMTP / IMAP / STUN simulados (sin red), omitidas sin
configuración, sin secretos en los resultados, guardado + conteos, CLI, permisos y aislamiento por empresa."""

import json
import smtplib

import httpx
import pytest
from sqlalchemy import func, select

from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    AIConnection,
    Agent,
    Channel,
    IntegrationConnection,
    Organization,
    Plan,
    SSOConnection,
    SystemCheck,
    SystemCheckRun,
    utcnow,
)
from app.preflight import checks_email, checks_infra, checks_misc, core
from app.preflight.__main__ import main as cli_main
from app.preflight.core import Context, execute
from app.secrets_vault import put_secret

ORG = 9330
OTHER = 9331
PWD = "Preflight-Clave-123"
SECRETS = ["EAAWHATSAPPSECRETTOKEN123", "sk_test_STRIPESECRET9876", "sk-ant-APIKEYSECRET5555", "smtp-pass-0001",
           "imap-pass-0002", "google-refresh-SECRET", "hubspot-access-SECRET", "sf-refresh-SECRET"]


# --- HTTP simulado ------------------------------------------------------------------------------------------
def _route(method: str, url: str, kw: dict) -> httpx.Response:
    j = lambda data, code=200: httpx.Response(code, json=data)  # noqa: E731
    if "debug_token" in url:
        token = (kw.get("params") or {}).get("input_token")
        if token == "EAABADTOKEN":
            return j({"data": {"is_valid": False}})
        return j({"data": {"is_valid": True, "app_id": "APP1", "type": "SYSTEM_USER", "expires_at": 0,
                           "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"]}})
    if url.endswith("/APP1"):
        return j({"name": "Mi App", "id": "APP1"})
    if url.endswith("/PN9330"):
        return j({"verified_name": "Concesionario", "display_phone_number": "+57 300 000 9330",
                  "quality_rating": "YELLOW", "platform_type": "CLOUD_API", "name_status": "APPROVED",
                  "code_verification_status": "VERIFIED", "throughput": {"level": "STANDARD"}})
    if url.endswith("/WABA9330/subscribed_apps"):
        return j({"data": [{"whatsapp_business_api_data": {"id": "APP1", "name": "Mi App"}}]})
    if url.endswith("/WABA9330/message_templates"):
        return j({"data": [{"name": "hola", "status": "APPROVED"}]})
    if url.endswith("/act_777"):
        return j({"name": "Cuenta Ads", "account_status": 1, "currency": "COP"})
    if url == "https://oauth2.googleapis.com/token":
        return j({"access_token": "ya29.GOOGLEACCESS", "expires_in": 3600})
    if url.endswith("customers:listAccessibleCustomers"):
        return j({"resourceNames": ["customers/1234567890"]})
    if url.endswith("/customers/1234567890/googleAds:search"):
        q = (kw.get("json") or {}).get("query", "")
        if "conversion_action" in q:
            return j({"results": []})
        return j({"results": [{"customer": {"id": "1234567890", "descriptiveName": "Ads CO", "currencyCode": "COP"}}]})
    if "api.hubapi.com/oauth/v1/access-tokens/" in url:
        return j({"hub_id": 42, "scopes": ["crm.objects.contacts.read", "crm.objects.contacts.write"]})
    if url.endswith("/services/oauth2/token"):
        return j({"access_token": "00DSFACCESS", "instance_url": "https://acme.my.salesforce.com"})
    if url.endswith("/services/data/v61.0/limits"):
        return j({"DailyApiRequests": {"Max": 15000, "Remaining": 900}})
    if url == "https://api.stripe.com/v1/account":
        return j({"id": "acct_1", "charges_enabled": False})
    if url == "https://api.stripe.com/v1/webhook_endpoints":
        return j({"data": [{"url": f"{get_settings().public_base_url.rstrip('/')}/api/billing/webhook",
                            "status": "enabled", "enabled_events": ["checkout.session.completed", "invoice.paid"]}]})
    if url.startswith("https://api.stripe.com/v1/prices/"):
        return j({"id": url.rsplit("/", 1)[-1], "active": url.endswith("price_ok")})
    if url == "https://idp.example.com/.well-known/openid-configuration":
        return j({"issuer": "https://idp.example.com", "jwks_uri": "https://idp.example.com/jwks"})
    if url == "https://idp.example.com/jwks":
        return j({"keys": [{"kid": "k1"}]})
    if url == "https://api.anthropic.com/v1/models":
        if kw.get("headers", {}).get("x-api-key") == "sk-ant-REVOKED0000":
            return j({"error": {"message": "invalid x-api-key"}}, 401)
        return j({"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}]})
    if url == "https://www.google.com":
        return httpx.Response(200, headers={"date": utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")})
    return j({"error": {"message": f"ruta no simulada {method} {url}"}}, 404)


class FakeHTTP:
    calls: list[tuple[str, str]] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        FakeHTTP.calls.append((method, url))
        return _route(method, url, kw)

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)

    async def head(self, url, **kw):
        return await self.request("HEAD", url, **kw)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeHTTP.calls = []
    monkeypatch.setattr(core, "http_client", lambda timeout=8.0: FakeHTTP())
    monkeypatch.setattr(checks_infra, "listen_roundtrip", _true)
    monkeypatch.setattr(checks_infra, "resolve", _true)
    monkeypatch.setattr(checks_misc, "stun_probe", lambda host, port, timeout=3.0: "203.0.113.7")
    s = get_settings()
    for k in ("meta_app_id", "meta_app_secret", "meta_embedded_signup_config_id", "wa_access_token", "meta_capi_token",
              "stripe_secret_key", "stripe_webhook_secret", "smtp_host", "smtp_from", "vapid_public_key",
              "vapid_private_key", "anthropic_api_key", "openai_api_key", "google_oauth_client_id",
              "google_oauth_client_secret", "google_ads_developer_token", "hubspot_client_id", "salesforce_client_id"):
        monkeypatch.setattr(s, k, "")


async def _true(*a, **k):
    return True


def _by_key(results) -> dict:
    return {r.check_key: r for r in results}


async def _seed_org() -> None:
    async with SessionLocal() as s:
        if await s.get(Organization, ORG):
            return
        s.add_all([Organization(id=ORG, name="Org diagnóstico", slug="org-preflight", onboarding_completed_at=utcnow()),
                   Organization(id=OTHER, name="Otra org", slug="otra-preflight", onboarding_completed_at=utcnow())])
        await s.flush()
        s.add_all([Agent(organization_id=ORG, email="admin@pf9330.co", name="Admin", role="admin",
                         password_hash=hash_password(PWD)),
                   Agent(organization_id=ORG, email="asesor@pf9330.co", name="Asesor", role="agent",
                         password_hash=hash_password(PWD)),
                   Agent(organization_id=OTHER, email="admin@pf9331.co", name="Admin 2", role="admin",
                         password_hash=hash_password(PWD))])
        ch = Channel(organization_id=ORG, name="WA diag", phone_number_id="PN9330", waba_id="WABA9330")
        bad = Channel(organization_id=ORG, name="WA sin acceso", phone_number_id="PN9339", waba_id=None)
        mail = Channel(organization_id=ORG, name="Ventas correo", provider="email", external_id="ventas@acme.co")
        s.add_all([ch, bad, mail])
        await s.flush()
        ch.access_token_secret_id = await put_secret(s, "EAAWHATSAPPSECRETTOKEN123", f"pf:wa:{ch.id}")
        bad.access_token_secret_id = await put_secret(s, "EAABADTOKEN", f"pf:wa:{bad.id}")
        mail.settings = {"inbound": "imap",
                         "smtp": {"host": "smtp.acme.co", "port": 587, "user": "ventas@acme.co",
                                  "password_secret_id": await put_secret(s, "smtp-pass-0001", f"pf:smtp:{mail.id}")},
                         "imap": {"host": "imap.acme.co", "port": 993, "user": "ventas@acme.co",
                                  "password_secret_id": await put_secret(s, "imap-pass-0002", f"pf:imap:{mail.id}")}}
        g = IntegrationConnection(organization_id=ORG, provider="google_ads", external_account_id="123-456-7890")
        g.refresh_token_secret_id = await put_secret(s, "google-refresh-SECRET", "pf:g")
        hs = IntegrationConnection(organization_id=ORG, provider="hubspot", expires_at=utcnow().replace(year=2099))
        hs.access_token_secret_id = await put_secret(s, "hubspot-access-SECRET", "pf:hs")
        sf = IntegrationConnection(organization_id=ORG, provider="salesforce", instance_url="https://acme.my.salesforce.com")
        sf.refresh_token_secret_id = await put_secret(s, "sf-refresh-SECRET", "pf:sf")
        meta = IntegrationConnection(organization_id=ORG, provider="meta", settings={"ad_account_id": "777"})
        meta.access_token_secret_id = await put_secret(s, "EAAWHATSAPPSECRETTOKEN123", "pf:meta")
        ai = AIConnection(organization_id=ORG, name="Claude", provider="anthropic", model="claude-opus-5-5")
        ai.api_key_secret_id = await put_secret(s, "sk-ant-APIKEYSECRET5555", "pf:ai")
        s.add_all([g, hs, sf, meta, ai,
                   SSOConnection(organization_id=ORG, protocol="oidc", name="OneLogin", slug="pf-onelogin",
                                 issuer="https://idp.example.com", client_id="cid", is_active=True)])
        await s.commit()


# --- Pruebas -------------------------------------------------------------------------------------------------
async def test_unconfigured_platform_checks_are_skipped():
    results = _by_key(await execute(Context(organization_id=None), "platform",
                                    ["meta", "stripe", "email", "push", "ai", "supabase"]))
    for key in ("meta.app", "stripe.account", "email.smtp", "push.vapid", "ai.server_keys", "supabase.storage"):
        assert results[key].status == "skipped", key
        assert results[key].detail  # siempre explica cómo activarlo


async def test_platform_checks_with_fakes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "meta_app_id", "APP1")
    monkeypatch.setattr(s, "meta_app_secret", "appsecret")
    monkeypatch.setattr(s, "wa_access_token", "EAAWHATSAPPSECRETTOKEN123")
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_STRIPESECRET9876")
    monkeypatch.setattr(s, "anthropic_api_key", "sk-ant-APIKEYSECRET5555")
    monkeypatch.setattr(s, "smtp_host", "smtp.server.co")
    monkeypatch.setattr(s, "jwt_secret", "x" * 40)
    async with SessionLocal() as session:
        plan = await session.scalar(select(Plan).where(Plan.is_public).order_by(Plan.id).limit(1))
        original_price = plan.provider_price_id
        plan.provider_price_id = "price_bad"
        await session.commit()

    seen: list = []

    def fake_smtp(host, port, user, password, starttls=True):
        seen.append((host, port))
        raise smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(checks_email, "smtp_probe", fake_smtp)

    try:
        results = await execute(Context(organization_id=None), "platform", None)
    finally:
        async with SessionLocal() as session:
            p = await session.get(Plan, plan.id)
            p.provider_price_id = original_price
            await session.commit()
    r = _by_key(results)
    assert r["meta.app"].status == "pass" and r["meta.embedded_signup"].status == "warn"
    assert r["meta.wa_access_token"].status == "pass"
    assert r["meta.verify_token"].status == "fail" and r["meta.app_secret"].status == "fail"
    assert r["stripe.account"].status == "pass"
    assert r["stripe.webhook"].status == "fail" and "faltan eventos" in r["stripe.webhook"].detail.lower()
    assert r[f"stripe.price:{plan.key}"].status == "fail"
    assert r["email.smtp"].status == "fail" and "contraseña de aplicación" in r["email.smtp"].detail
    assert seen == [("smtp.server.co", 587)]
    assert r["ai.server_key:anthropic"].status == "pass"
    assert r["infra.database"].status == "pass" and r["infra.pooler_listen"].status == "pass"
    assert r["infra.jwt_secret"].status == "pass" and r["infra.clock"].status == "pass"
    assert r["voice.stun:0"].status == "pass" and r["voice.stun:0"].data["public_ip"] == "203.0.113.7"
    assert r["supabase.vault"].status == "pass"  # ida y vuelta real contra el Vault de prueba
    # Ningún secreto en los resultados
    dump = json.dumps([x.out() for x in results], default=str, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in dump, secret
    assert "••••" in dump


async def test_vapid_keys(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.preflight.checks_misc import vapid

    import base64

    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.private_numbers().private_value.to_bytes(32, "big")
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    s = get_settings()
    monkeypatch.setattr(s, "vapid_private_key", b64(raw))
    monkeypatch.setattr(s, "vapid_public_key", b64(pub))
    monkeypatch.setattr(s, "vapid_subject", "mailto:soporte@acme.co")
    assert (await vapid(Context(organization_id=None)))[0].status == "pass"
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    monkeypatch.setattr(s, "vapid_public_key", b64(other))
    assert (await vapid(Context(organization_id=None)))[0].status == "fail"


async def test_org_checks_with_fakes(monkeypatch):
    await _seed_org()
    s = get_settings()
    for k, v in (("meta_app_id", "APP1"), ("meta_app_secret", "appsecret"), ("google_oauth_client_id", "gid"),
                 ("google_oauth_client_secret", "gsecret"), ("google_ads_developer_token", "devtoken"),
                 ("salesforce_client_id", "sfid")):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(checks_email, "smtp_probe", lambda *a, **k: "STARTTLS")

    def fake_imap(host, port, user, password, folder, ssl=True):
        assert password == "imap-pass-0002"  # la contraseña viene del Vault, pero nunca sale en el resultado
        return 12
    monkeypatch.setattr(checks_email, "imap_probe", fake_imap)

    results = await execute(Context(organization_id=ORG), "org", None)
    r = _by_key(results)
    async with SessionLocal() as session:
        ch = await session.scalar(select(Channel).where(Channel.phone_number_id == "PN9330"))
        bad = await session.scalar(select(Channel).where(Channel.phone_number_id == "PN9339"))
        mail = await session.scalar(select(Channel).where(Channel.external_id == "ventas@acme.co"))
    assert r[f"meta.wa_token:{ch.id}"].status == "pass"
    assert r[f"meta.wa_phone:{ch.id}"].status == "warn" and "YELLOW" in r[f"meta.wa_phone:{ch.id}"].detail
    assert r[f"meta.wa_subscribed:{ch.id}"].status == "pass" and r[f"meta.wa_templates:{ch.id}"].status == "pass"
    assert r[f"meta.wa_token:{bad.id}"].status == "fail"
    assert r[f"meta.wa_subscribed:{bad.id}"].status == "warn"  # sin WABA ID
    assert r["meta.ads"].status == "pass"
    assert r["google_ads.oauth"].status == "pass" and r["google_ads.customer"].status == "pass"
    assert r["google_ads.conversion_actions"].status == "warn"
    assert r["hubspot.connection"].status == "fail" and "deals" in r["hubspot.connection"].detail
    assert r["salesforce.connection"].status == "warn"  # 6 % de llamadas API restantes
    assert r[f"email.channel_smtp:{mail.id}"].status == "pass" and r[f"email.channel_imap:{mail.id}"].status == "pass"
    sso = next(x for x in results if x.check_key.startswith("sso.connection:"))
    assert sso.status == "pass"
    ai = next(x for x in results if x.check_key.startswith("ai.connection:"))
    assert ai.status == "pass"
    dump = json.dumps([x.out() for x in results], default=str, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in dump, secret
    # Solo lectura: ninguna llamada de escritura a Meta / Stripe / Google aparte de los tokens OAuth
    writes = [(m, u) for m, u in FakeHTTP.calls if m in ("POST", "DELETE")]
    assert all(u.endswith(("/token", "googleAds:search")) for _m, u in writes), writes


async def test_save_upserts_and_counts():
    _run, results = await core.run_and_save(None, "platform", ["push", "stripe"], trigger="cli")
    _run2, _ = await core.run_and_save(None, "platform", ["push", "stripe"], trigger="cli")
    async with SessionLocal() as s:
        n = await s.scalar(select(func.count()).select_from(SystemCheck).where(
            SystemCheck.organization_id.is_(None), SystemCheck.check_key == "push.vapid"))
        run = await s.get(SystemCheckRun, _run2.id)
    assert n == 1  # se actualiza, no se duplica
    assert run.finished_at and run.skipped == len(results)


def test_cli_exit_codes(monkeypatch, capsys):
    # CLI: todo omitido → 0; VAPID inválido → 2
    assert cli_main(["--areas", "push", "--no-save"]) == 0
    monkeypatch.setattr(get_settings(), "vapid_public_key", "AAAA")
    monkeypatch.setattr(get_settings(), "vapid_private_key", "no-es-una-llave")
    assert cli_main(["--areas", "push", "--no-save", "--json"]) == 2
    out = capsys.readouterr().out
    assert '"exit_code": 2' in out and "no-es-una-llave" not in out
    assert cli_main(["--areas", "nada"]) == 2


async def test_api_permissions_and_isolation():
    await _seed_org()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        async def login(email):
            r = await c.post("/api/auth/login", json={"email": email, "password": PWD})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        admin, agent, other = (await login("admin@pf9330.co"), await login("asesor@pf9330.co"),
                               await login("admin@pf9331.co"))
        assert (await c.post("/api/preflight/run", json={"areas": ["sso"]}, headers=agent)).status_code == 403
        r = await c.post("/api/preflight/run", json={"areas": ["sso"]}, headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "done" and body["run"]["finished_at"]
        assert any(a["area"] == "sso" for a in body["areas"])
        assert body["platform"] is None  # instalación SaaS (varias empresas): el back-office ve lo del servidor
        mine = (await c.get("/api/preflight", headers=admin)).json()
        assert any(x["check_key"].startswith("sso.connection:") for a in mine["areas"] for x in a["results"])
        theirs = (await c.get("/api/preflight", headers=other)).json()
        assert not any(x["check_key"].startswith("sso.connection:") for a in theirs["areas"] for x in a["results"])
        assert (await c.post("/api/platform/preflight/run", headers=admin)).status_code in (401, 403)
