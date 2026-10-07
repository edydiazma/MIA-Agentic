"""SaaS multiempresa: registro, aislamiento, varias empresas por correo, límites, funcionalidades por plan,
facturación con Stripe (firma + idempotencia), back-office y resolución de la empresa en webhooks."""

import hashlib
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.billing.stripe import StripeProvider
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import InboundEvent, Organization, Plan
from app.routers import billing as billing_router
from app.routers import signup as signup_router

WHSEC = "whsec_test_123"


@pytest.fixture(autouse=True)
def reset_rate_limit():
    signup_router._signups.clear()


@pytest.fixture
def platform_env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "platform_admin_email", "dueno@plataforma.test")
    monkeypatch.setattr(s, "platform_admin_password", "plataforma123")


@pytest.fixture
def stripe(monkeypatch):
    calls = []

    async def fake_post(self, path, data):
        calls.append((path, data))
        if path == "/customers":
            return {"id": "cus_test"}
        if path == "/checkout/sessions":
            return {"id": "cs_test", "url": "https://checkout.stripe.test/cs_test"}
        return {"url": "https://billing.stripe.test/portal"}

    monkeypatch.setattr(StripeProvider, "_post", fake_post)
    monkeypatch.setattr(billing_router, "provider", lambda: StripeProvider(secret_key="sk_test", webhook_secret=WHSEC))
    return calls


def _anon() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _signup(c, company, email, password="clave-segura-1", plan_key="team") -> dict:
    r = await c.post("/api/signup", json={"company": company, "name": "Dueña", "email": email, "password": password,
                                          "country": "co", "plan_key": plan_key})
    assert r.status_code == 200, r.text
    return r.json()


def _signed(event: dict, secret: str = WHSEC, ts: int | None = None) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


async def test_signup_isolation_plan_limits_and_features(client):
    async with _anon() as anon:
        cfg = (await anon.get("/api/signup/config")).json()
        assert cfg["enabled"] and {p["key"] for p in cfg["plans"]} >= {"team", "professional", "enterprise"}
        new = await _signup(anon, "Concesionario Norte", "norte@cliente.test")
    token = new["access_token"]
    assert new["organization"]["status"] == "trial"

    # Aislamiento: no ve datos de la empresa 1
    contact = (await client.post("/api/contacts", json={"wa_id": "573001231234", "name": "De la empresa 1"})).json()
    async with _anon() as c:
        c.headers.update(_auth(token))
        assert (await c.get(f"/api/contacts/{contact['id']}")).status_code == 404
        assert (await c.get("/api/contacts")).json()["total"] == 0
        assert (await c.get("/api/conversations")).json() == []

        plan = (await c.get("/api/plan")).json()
        assert plan["plan"]["key"] == "team" and plan["organization"]["trial_days_left"] == 14
        assert plan["usage"]["channels"] == {"used": 0.0, "limit": 2.0, "allowed": True}

        # Límite duro: el plan Team permite 2 números
        for i in (1, 2):
            r = await c.post("/api/channels", json={"name": f"WA {i}", "phone_number_id": f"PN-NORTE-{i}"})
            assert r.status_code == 200, r.text
        r = await c.post("/api/channels", json={"name": "WA 3", "phone_number_id": "PN-NORTE-3"})
        assert r.status_code == 402 and "2 números" in r.json()["detail"]

        # Funcionalidad fuera del plan: «Mejor vendedor» no está en Team; «Memoria» sí
        assert (await c.get("/api/seller/ranking")).status_code == 402
        assert (await c.get("/api/memory/items")).status_code == 200

    # La empresa 1 (sin plan asignado) no tiene límites
    assert (await client.get("/api/seller/ranking")).status_code == 200


async def test_multi_org_login_and_switch(client):
    email, password = "multi@empresas.test", "Misma-Clave-2026"
    r = await client.post("/api/agents", json={"email": email, "name": "Multi", "password": password, "role": "agent"})
    assert r.status_code == 200, r.text
    async with _anon() as anon:
        new = await _signup(anon, "Agencia Multi", email, password)
        r = await anon.post("/api/auth/login", json={"email": email, "password": password})
        choices = r.json()["choose_org"]
        assert {o["id"] for o in choices} == {1, new["organization"]["id"]}

        r = await anon.post("/api/auth/login", json={"email": email, "password": password, "organization_id": 1})
        token1 = r.json()["access_token"]
        assert r.json()["organization"]["id"] == 1
        orgs = (await anon.get("/api/auth/orgs", headers=_auth(token1))).json()
        assert len(orgs["organizations"]) == 2

        r = await anon.post("/api/auth/switch-org", json={"organization_id": new["organization"]["id"]},
                            headers=_auth(token1))
        assert r.status_code == 200 and r.json()["agent"]["role"] == "admin"
        # El token de una empresa no sirve para cambiarse a otra que no validó con contraseña
        r = await anon.post("/api/auth/switch-org", json={"organization_id": 999999}, headers=_auth(token1))
        assert r.status_code == 403

        # Mismo correo, contraseña distinta: solo entra a la empresa cuya contraseña coincide
        other = await _signup(anon, "Otra Con Mismo Correo", email, "otra-clave-77")
        r = await anon.post("/api/auth/login", json={"email": email, "password": "otra-clave-77"})
        assert r.json()["organization"]["id"] == other["organization"]["id"]
        # ...y desde esa sesión no puede saltar a las otras dos
        r2 = await anon.post("/api/auth/switch-org", json={"organization_id": 1},
                             headers=_auth(r.json()["access_token"]))
        assert r2.status_code == 403


async def test_stripe_billing_webhook(client, stripe):
    async with _anon() as anon:
        new = await _signup(anon, "Paga Bien", "paga@cliente.test")
    org_id, token = new["organization"]["id"], new["access_token"]
    async with _anon() as c:
        c.headers.update(_auth(token))
        assert (await c.post("/api/billing/checkout", json={"plan_key": "professional"})).status_code == 422
        async with SessionLocal() as s:
            for p in (await s.scalars(select(Plan))).all():
                p.provider_price_id = f"price_{p.key}"
            await s.commit()
        r = await c.post("/api/billing/checkout", json={"plan_key": "professional"})
        assert r.json()["url"].startswith("https://checkout.stripe.test")
        checkout = next(d for p, d in stripe if p == "/checkout/sessions")
        assert checkout["metadata"]["org_id"] == org_id and checkout["line_items"][0]["price"] == "price_professional"

        event = {"id": "evt_1", "type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_1", "object": "subscription", "customer": "cus_test", "status": "active",
            "current_period_end": int(time.time()) + 30 * 86400, "metadata": {"org_id": str(org_id)},
            "items": {"data": [{"price": {"id": "price_enterprise"}}]}}}}
        body, headers = _signed(event)
        assert (await c.post("/api/billing/webhook", content=body, headers=headers)).json()["result"] == "processed"
        assert (await c.post("/api/billing/webhook", content=body, headers=headers)).json()["result"] == "duplicate"
        bad_body, bad_headers = _signed(event, secret="otro")
        assert (await c.post("/api/billing/webhook", content=bad_body, headers=bad_headers)).status_code == 400
        old_body, old_headers = _signed(event, ts=int(time.time()) - 3600)
        assert (await c.post("/api/billing/webhook", content=old_body, headers=old_headers)).status_code == 400

        plan = (await c.get("/api/plan")).json()
        assert plan["organization"]["status"] == "active" and plan["plan"]["key"] == "enterprise"
        assert plan["subscription"]["status"] == "active"

        failed = {"id": "evt_2", "type": "invoice.payment_failed", "data": {"object": {
            "object": "invoice", "customer": "cus_test", "subscription": "sub_1"}}}
        body, headers = _signed(failed)
        await c.post("/api/billing/webhook", content=body, headers=headers)
        assert (await c.get("/api/plan")).json()["organization"]["status"] == "past_due"
        assert (await c.get("/api/conversations")).status_code == 200  # en mora sigue operando (con aviso)

        deleted = {"id": "evt_3", "type": "customer.subscription.deleted", "data": {"object": {
            "id": "sub_1", "object": "subscription", "customer": "cus_test", "status": "canceled",
            "metadata": {"org_id": str(org_id)}}}}
        body, headers = _signed(deleted)
        await c.post("/api/billing/webhook", content=body, headers=headers)
        assert (await c.get("/api/conversations")).status_code == 402
        assert (await c.get("/api/plan")).status_code == 200  # puede ver su plan y volver a pagar
        assert (await c.post("/api/billing/portal")).json()["url"].startswith("https://billing.stripe.test")


async def test_platform_backoffice(client, platform_env):
    async with _anon() as anon:
        new = await _signup(anon, "Cliente Backoffice", "bo@cliente.test")
        r = await anon.post("/api/platform/login", json={"email": "dueno@plataforma.test", "password": "plataforma123"})
        assert r.status_code == 200, r.text
        ptoken = r.json()["access_token"]
        p = _auth(ptoken)
        # Los tokens no se mezclan
        assert (await anon.get("/api/conversations", headers=p)).status_code == 401
        assert (await anon.get("/api/platform/orgs", headers=_auth(new["access_token"]))).status_code == 401

        orgs = (await anon.get("/api/platform/orgs", headers=p)).json()
        row = next(o for o in orgs if o["id"] == new["organization"]["id"])
        assert row["status"] == "trial" and row["plan"]["key"] == "team" and row["users"] == 1
        metrics = (await anon.get("/api/platform/metrics", headers=p)).json()
        assert metrics["organizations"] >= 2 and "trial" in metrics["by_status"]

        enterprise = next(pl for pl in (await anon.get("/api/platform/plans", headers=p)).json() if pl["key"] == "enterprise")
        await anon.put(f"/api/platform/orgs/{row['id']}", json={"plan_id": enterprise["id"]}, headers=p)
        assert (await anon.get("/api/plan", headers=_auth(new["access_token"]))).json()["plan"]["key"] == "enterprise"

        # Suspender bloquea todo menos su plan
        await anon.put(f"/api/platform/orgs/{row['id']}", json={"status": "suspended"}, headers=p)
        assert (await anon.get("/api/conversations", headers=_auth(new["access_token"]))).status_code == 403
        assert (await anon.get("/api/plan", headers=_auth(new["access_token"]))).status_code == 200
        # Prueba vencida: 402 hasta extenderla
        await anon.put(f"/api/platform/orgs/{row['id']}", json={"status": "trial", "extend_trial_days": 3}, headers=p)
        assert (await anon.get("/api/conversations", headers=_auth(new["access_token"]))).status_code == 200

        team = next(pl for pl in (await anon.get("/api/platform/plans", headers=p)).json() if pl["key"] == "team")
        r = await anon.put(f"/api/platform/plans/{team['id']}", json={"limits": {**team["limits"], "channels": 4}},
                           headers=p)
        assert r.json()["limits"]["channels"] == 4
        assert (await anon.put(f"/api/platform/plans/{team['id']}", json={"limits": {"nope": 1}},
                               headers=p)).status_code == 422
        await anon.put(f"/api/platform/plans/{team['id']}", json={"limits": team["limits"]}, headers=p)


async def test_webhook_resolves_organization_from_number(client):
    async with _anon() as anon:
        new = await _signup(anon, "Webhook Org", "wh@cliente.test")
        await anon.post("/api/channels", json={"name": "WA", "phone_number_id": "PN-WH-ORG"},
                        headers=_auth(new["access_token"]))
        for pnid in ("PN-WH-ORG", "PN-DESCONOCIDO"):
            payload = {"entry": [{"id": "WABA-X", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": pnid}, "statuses": []}}]}]}
            assert (await anon.post("/webhooks/whatsapp", json=payload)).status_code == 200
    async with SessionLocal() as s:
        rows = (await s.scalars(select(InboundEvent).where(InboundEvent.source == "whatsapp")
                                .order_by(InboundEvent.id.desc()).limit(2))).all()
        org = await s.get(Organization, new["organization"]["id"])
    assert rows[0].organization_id is None  # número desconocido: nunca se asume la empresa por defecto
    assert rows[1].organization_id == org.id
