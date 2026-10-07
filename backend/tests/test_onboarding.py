"""Asistente de onboarding: conexión con Meta (falsa), validaciones y arreglos, perfil, plantillas (aprobación por
webhook, rechazo y reescritura con IA), importación del sitio, prueba de ida y vuelta, salida en vivo,
invitaciones y aislamiento entre empresas."""

import re
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentGroup,
    AgentInvitation,
    Automation,
    Group,
    KnowledgeDoc,
    OnboardingRun,
    Organization,
    Plan,
    Product,
    WaTemplate,
    utcnow,
)
from app.onboarding import graph, service, website
from app.tenancy import create_org
from tests.conftest import settle, text

WABA, PNID, APP_ID = "WABA-ONB", "PN-ONB-1", "APP-1"


class FakeGraph:
    """Meta simulada: guarda lo que se le pide y responde como la Graph API."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.subscribed = False
        self.registered = False
        self.templates: dict[str, dict] = {}
        self.profile: dict = {}
        self.fail: dict[str, graph.GraphError] = {}
        self.phone = {"display_phone_number": "+57 300 555 0101", "verified_name": "Autos Andinos",
                      "code_verification_status": "VERIFIED", "quality_rating": "GREEN",
                      "name_status": "APPROVED", "messaging_limit_tier": "TIER_1K", "throughput": {"level": "STANDARD"}}

    async def call(self, method, path, token=None, params=None, json=None, data=None, headers=None):
        self.calls.append((method, path, json))
        for key, err in self.fail.items():
            if key in path:
                raise err
        if path == "oauth/access_token":
            return {"access_token": "TOKEN-ONB"}
        if path.endswith("/subscribed_apps"):
            if method == "POST":
                self.subscribed = True
                return {"success": True}
            return {"data": [{"whatsapp_business_api_data": {"id": APP_ID}}] if self.subscribed else []}
        if path.endswith("/register"):
            self.registered = True
            return {"success": True}
        if path.endswith("/whatsapp_business_profile"):
            if method == "POST":
                self.profile = json
                return {"success": True}
            return {"data": [self.profile]}
        if path.endswith("/message_templates"):
            if method == "POST":
                tid = f"T{len(self.templates) + 1}"
                self.templates[json["name"]] = {"id": tid, "name": json["name"], "language": json["language"],
                                                "status": "PENDING", "category": json["category"]}
                return {"id": tid, "status": "PENDING", "category": json["category"]}
            return {"data": list(self.templates.values())}
        if path.endswith("/messages"):
            return {"messages": [{"id": "wamid.TEST"}]}
        if path.startswith("PN-ONB") and "/" not in path and method == "GET":
            return {**self.phone, "platform_type": "CLOUD_API" if self.registered else "NOT_APPLICABLE"}
        if path.startswith("PN-ONB") and "/" not in path and method == "POST":
            return {"success": True}
        return {}


@pytest.fixture
def meta(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(graph, "call", fake.call)
    s = get_settings()
    monkeypatch.setattr(s, "meta_app_id", APP_ID)
    monkeypatch.setattr(s, "meta_app_secret", "SECRET")
    monkeypatch.setattr(s, "meta_embedded_signup_config_id", "CFG")
    return fake


@asynccontextmanager
async def org_client(name: str, email: str, plan_limits: dict | None = None):
    async with SessionLocal() as s:
        org = await create_org(s, name, "CO", None, status="active")
        if plan_limits is not None:
            plan = Plan(key="test_" + re.sub(r"[^a-z0-9]+", "_", email), name="Test", limits=plan_limits, features={}, is_public=False)
            s.add(plan)
            await s.flush()
            org.plan_id = plan.id
        s.add(Agent(organization_id=org.id, email=email, name="Admin", role="admin",
                    password_hash=hash_password("secret123")))
        await s.commit()
        org_id = org.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/auth/login", json={"email": email, "password": "secret123"})
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c, org_id


def _status(webhook_event: str, name: str, reason: str | None = None) -> dict:
    v = {"event": webhook_event, "message_template_name": name, "message_template_language": "es"}
    if reason:
        v["reason"] = reason
    return {"entry": [{"id": WABA, "changes": [{"field": "message_template_status_update", "value": v}]}]}


async def test_onboarding_happy_path(meta, monkeypatch):
    async with org_client("Autos Andinos", "admin.onb@test.com") as (c, org):
        st = (await c.get("/api/onboarding")).json()
        assert st["run"]["status"] == "in_progress" and st["run"]["current_step"] == "company"
        assert [x["key"] for x in st["steps"]][:3] == ["company", "whatsapp", "validate"]
        assert st["embedded_signup"]["enabled"] is True and st["org"]["onboarding_completed_at"] is None

        # Empresa (formato del panel: horario por día) e importación del sitio
        pages = {"https://autos.test": '<title>Autos Andinos</title><a href="/contacto">Contacto</a><p>Venta de carros</p>',
                 "https://autos.test/contacto": "<p>Calle 1 #2-3, ventas@autos.test</p>"}

        async def fake_fetch(url):
            return url, pages[url.rstrip("/")]

        async def fake_json(org_id, cortex_id, purpose, system, user, schema, **kw):
            assert purpose == "onboarding" and "Calle 1" in user
            return {"profile": {"name": "Autos Andinos", "description": "Concesionario en Bogotá", "about": "Carros nuevos",
                                "address": "Calle 1 #2-3", "email": "ventas@autos.test", "phone": None,
                                "website": None, "hours": None, "tone": "cercano", "vertical": "automotriz"},
                    "faqs": [{"question": "¿Reciben carro usado?", "answer": "Sí, como parte de pago."}],
                    "products": [{"name": "Onix LTZ", "description": None, "price": 89900000, "currency": "COP",
                                  "url": None, "image_url": None}]}, None

        monkeypatch.setattr(website, "fetch", fake_fetch)
        monkeypatch.setattr(website, "json_call", fake_json)
        imp = (await c.post("/api/onboarding/import-website", json={"url": "autos.test"})).json()
        assert imp["pages_read"] == 2 and imp["profile"]["website"] == "https://autos.test"
        assert imp["profile"]["phone"] is None  # lo que no está en el sitio no se inventa

        r = await c.put("/api/onboarding/answers", json={"current_step": "whatsapp", "answers": {"company": {
            "name": "Autos Andinos", "vertical": "automotriz", "website": "https://autos.test",
            "address": "Calle 1 #2-3", "email": "ventas@autos.test", "description": "Concesionario en Bogotá",
            "hours": {"mon": {"open": True, "from": "08:00", "to": "18:00"},
                      "sat": {"open": True, "from": "09:00", "to": "13:00"}, "sun": {"open": False}}}}})
        assert r.status_code == 200, r.text
        assert r.json()["industry"] == "automotriz" and r.json()["current_step"] == "whatsapp"

        # Conexión con Meta: token, suscripción, registro con PIN generado
        r = await c.post("/api/onboarding/whatsapp", json={"code": "CODE", "waba_id": WABA, "phone_number_id": PNID})
        assert r.status_code == 200, r.text
        out = r.json()
        assert re.fullmatch(r"\d{6}", out["pin"]) and out["channel"]["verified_name"] == "Autos Andinos"
        assert meta.subscribed and meta.registered

        st = (await c.get("/api/onboarding")).json()
        checks = {x["check_key"]: x for x in st["checks"]}
        assert checks["token_valid"]["status"] == "pass" and checks["webhook"]["status"] == "pass"
        assert checks["registered"]["status"] == "pass" and checks["quality"]["status"] == "pass"
        assert checks["templates_ready"]["status"] == "fail" and checks["templates_ready"]["fixable"]
        steps = {x["key"]: x for x in st["steps"]}
        assert steps["validate"]["status"] == "warning" and "profile" in steps["validate"]["result"]["warnings"]

        # Salir en vivo sin plantillas: bloqueado con motivos
        r = await c.post("/api/onboarding/go-live")
        assert r.status_code == 409 and "templates_ready" in r.json()["blocking"]

        # Perfil (desde las respuestas) y paquete de plantillas
        r = await c.post("/api/onboarding/steps/profile/run", json={})
        assert r.json()["step"]["status"] == "done"
        assert meta.profile["vertical"] == "AUTO" and meta.profile["websites"] == ["https://autos.test"]

        pack = (await c.get("/api/onboarding/template-pack")).json()
        assert pack["pack_key"] == "automotriz.basico"
        assert all("Autos Andinos" in t["body"] for t in pack["templates"] if t["template_key"] == "solicitud_recibida")
        r = await c.post("/api/onboarding/templates", json={})
        sub = r.json()
        assert not sub["errors"] and {x["template_key"] for x in sub["submitted"]} >= {"cotizacion_vehiculo",
                                                                                       "solicitud_recibida"}
        assert "novedades" not in {x["template_key"] for x in sub["submitted"]}  # marketing: opcional

        # Meta aprueba una y rechaza otra (webhook)
        await c.post("/webhooks/whatsapp", json=_status("APPROVED", "solicitud_recibida"))
        await c.post("/webhooks/whatsapp", json=_status("REJECTED", "retomar_conversacion", "INVALID_FORMAT"))
        await settle()
        st = (await c.get("/api/onboarding")).json()
        submitted = {x["template_key"]: x for x in next(s for s in st["steps"] if s["key"] == "templates")["result"]["submitted"]}
        assert submitted["solicitud_recibida"]["status"] == "APPROVED"
        assert submitted["retomar_conversacion"]["rejected_reason"] == "INVALID_FORMAT"

        # Reescritura con IA del rechazo → nombre nuevo _v2
        async def fake_rewrite(org_id, cortex_id, purpose, system, user, schema, **kw):
            assert "INVALID_FORMAT" in user
            return {"body": "Hola {{1}}, seguimos con tu consulta sobre {{2}} en Autos Andinos. Responde aquí.",
                    "category": "UTILITY", "example_values": ["Ana", "tu cotización"]}, None

        monkeypatch.setattr(service, "json_call", fake_rewrite)
        r = (await c.post("/api/onboarding/templates/retomar_conversacion/rewrite")).json()
        assert r["submitted"][0]["name"] == "retomar_conversacion_v2" and not r["errors"]

        # Agente de IA: instrucciones, FAQ, tipificaciones y grupos de la industria, horario, catálogo
        r = await c.put("/api/onboarding/answers", json={"answers": {
            "import": {"faqs": imp["faqs"], "products": imp["products"]},
            "ai": {"apply_groups": True, "hours_message": "Te respondemos en horario hábil."}}})
        r = await c.post("/api/onboarding/steps/ai/run", json={})
        assert r.json()["step"]["status"] == "done", r.text
        async with SessionLocal() as s:
            assert await s.scalar(select(KnowledgeDoc.id).where(KnowledgeDoc.organization_id == org))
            assert {g.name for g in (await s.scalars(select(Group).where(Group.organization_id == org))).all()} >= {
                "Ventas", "Posventa"}
            rule = await s.scalar(select(Automation).where(Automation.organization_id == org,
                                                           Automation.type == "business_hours"))
            assert rule.config["days"] == [0, 5] and rule.config["start"] == "08:00" and rule.config["end"] == "18:00"
            assert rule.config["message"] == "Te respondemos en horario hábil."
            assert await s.scalar(select(Product.id).where(Product.organization_id == org, Product.sku == "web-onix-ltz"))
        r = await c.post("/api/onboarding/steps/ai/run", json={})  # idempotente
        assert r.json()["step"]["status"] == "done"

        # Prueba de ida y vuelta: se envía la plantilla aprobada y el cliente responde
        r = (await c.post("/api/onboarding/test", json={"phone": "573001112233"})).json()
        assert r == {"sent": True, "message_id": "wamid.TEST", "waiting_reply": True}
        sent = next(j for (m, p, j) in meta.calls if p.endswith("/messages"))
        assert sent["template"]["name"] == "solicitud_recibida"
        msg = text("573001112233", "wamid.reply1", "Recibido 👍")
        msg["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = PNID
        await c.post("/webhooks/whatsapp", json=msg)
        await settle()
        st = (await c.get("/api/onboarding")).json()
        assert {x["key"]: x["status"] for x in st["steps"]}["test"] == "done"
        assert {x["check_key"]: x["status"] for x in st["checks"]}["inbound_roundtrip"] == "pass"

        # Salir en vivo
        r = await c.post("/api/onboarding/go-live")
        assert r.status_code == 200, r.text
        st = (await c.get("/api/onboarding")).json()
        assert st["run"]["status"] == "completed" and st["org"]["onboarding_completed_at"]
        health = (await c.get(f"/api/channels/{out['channel']['id']}/health")).json()["checks"]
        assert {h["check_key"] for h in health} >= {"token_valid", "webhook", "templates_ready"}


async def test_fix_actions_coexistence_and_manual_channel(meta):
    async with org_client("Clínica Sur", "admin.fix@test.com") as (c, org):
        await c.get("/api/onboarding")
        meta.phone["quality_rating"] = "YELLOW"
        r = await c.post("/api/onboarding/whatsapp", json={"code": "C", "waba_id": WABA + "2",
                                                           "phone_number_id": "PN-ONB-2", "coexistence": True})
        assert r.status_code == 200 and r.json()["pin"] is None and r.json()["channel"]["is_coexistence"]
        assert not meta.registered  # coexistencia: no se registra
        assert {j["sync_type"] for (m, p, j) in meta.calls if p.endswith("/smb_app_data")} == {
            "smb_app_state_sync", "history"}
        checks = {x["check_key"]: x for x in (await c.post("/api/onboarding/checks/run")).json()["checks"]}
        assert checks["registered"]["status"] == "pass" and checks["quality"]["status"] == "warn"

        # La suscripción se perdió → "Arreglar" la recupera
        meta.subscribed = False
        checks = {x["check_key"]: x for x in (await c.post("/api/onboarding/checks/run")).json()["checks"]}
        assert checks["webhook"]["status"] == "fail" and checks["webhook"]["fixable"]
        r = (await c.post("/api/onboarding/checks/webhook/fix")).json()
        assert r["check"]["status"] == "pass" and meta.subscribed
        r = (await c.post("/api/onboarding/checks/templates_ready/fix")).json()
        assert r["check"]["status"] == "warn"  # paquete enviado: en revisión
        r = await c.post("/api/onboarding/checks/token_valid/fix")
        assert r.status_code == 502

        # Errores de Meta llegan con su mensaje
        meta.fail["whatsapp_business_profile"] = graph.GraphError("Param about is too long", 100, 400)
        r = (await c.post("/api/onboarding/steps/profile/run", json={})).json()
        assert r["step"]["status"] == "failed" and "too long" in r["step"]["error"]

        # Pasos opcionales se pueden omitir, los obligatorios no
        assert (await c.post("/api/onboarding/steps/test/skip")).json()["step"]["status"] == "skipped"
        assert (await c.post("/api/onboarding/steps/templates/skip")).status_code == 409

        # Reiniciar el asistente conserva el número
        st = (await c.post("/api/onboarding/restart")).json()
        assert st["run"]["channel_id"] is not None and st["run"]["status"] == "in_progress"

    # Conexión manual: el canal se creó con POST /api/channels y el asistente lo adopta
    async with org_client("Tienda Norte", "admin.manual@test.com") as (c2, org2):
        await c2.get("/api/onboarding")
        r = await c2.post("/api/onboarding/steps/whatsapp/run", json={})
        assert r.status_code == 409
        ch = await c2.post("/api/channels", json={"name": "Ventas", "phone_number_id": "PN-ONB-3",
                                                  "waba_id": WABA + "3", "access_token": "TOKEN-MANUAL"})
        assert ch.status_code == 200, ch.text
        r = await c2.post("/api/onboarding/steps/whatsapp/run", json={})
        assert r.json()["step"]["status"] == "done"
        st = (await c2.get("/api/onboarding")).json()
        assert st["run"]["channel_id"] == ch.json()["id"] and st["checks"]


async def test_invitations_and_isolation(meta):
    async with org_client("Inmo Centro", "admin.inv@test.com", plan_limits={"users": 3}) as (c, org):
        async with SessionLocal() as s:
            g = Group(organization_id=org, name="Ventas")
            s.add(g)
            await s.commit()
            gid = g.id
        r = await c.post("/api/invitations", json={"invites": [
            {"email": "Luisa@Test.com", "name": "Luisa", "role": "supervisor", "group_ids": [gid]},
            {"email": "pedro@test.com", "role": "agent"}]})
        assert r.status_code == 200, r.text
        luisa, pedro = r.json()
        assert luisa["email"] == "luisa@test.com" and luisa["email_sent"] is False and "/invitacion/" in luisa["link"]
        # Cupo: 1 admin + 2 pendientes = 3 → una más supera el plan
        assert (await c.post("/api/invitations", json={"invites": [{"email": "x@test.com"}]})).status_code == 402
        assert (await c.post("/api/invitations", json={"invites": [{"email": "bad"}]})).status_code == 422

        token = luisa["link"].rsplit("/", 1)[1]
        pub = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        async with pub:
            info = (await pub.get(f"/api/invitations/accept/{token}")).json()
            assert info == {"organization": "Inmo Centro", "email": "luisa@test.com", "name": "Luisa",
                            "role": "supervisor", "expires_at": info["expires_at"]}
            r = await pub.post("/api/invitations/accept", json={"token": token, "name": "Luisa R", "password": "clave12345"})
            assert r.status_code == 200 and r.json()["agent"]["role"] == "supervisor" and r.json()["access_token"]
            assert (await pub.post("/api/invitations/accept", json={"token": token, "name": "x",
                                                                    "password": "clave12345"})).status_code == 410
            async with SessionLocal() as s:
                agent = await s.scalar(select(Agent).where(Agent.email == "luisa@test.com"))
                assert await s.get(AgentGroup, (agent.id, gid))
            # Revocada y vencida
            assert (await c.delete(f"/api/invitations/{pedro['id']}")).json()["status"] == "revoked"
            ptoken = pedro["link"].rsplit("/", 1)[1]
            assert (await pub.get(f"/api/invitations/accept/{ptoken}")).status_code == 410
            r = (await c.post("/api/invitations", json={"invites": [{"email": "ana@test.com"}]})).json()[0]
            async with SessionLocal() as s:
                await s.execute(update(AgentInvitation).where(AgentInvitation.id == r["id"])
                                .values(expires_at=utcnow() - timedelta(minutes=1)))
                await s.commit()
            assert (await pub.get(f"/api/invitations/accept/{r['link'].rsplit('/', 1)[1]}")).status_code == 410
        statuses = {i["email"]: i["status"] for i in (await c.get("/api/invitations")).json()}
        assert statuses == {"luisa@test.com": "accepted", "pedro@test.com": "revoked", "ana@test.com": "expired"}

    # Otra empresa no ve ni toca lo de esta
    async with org_client("Otra SA", "admin.otra@test.com") as (c2, _):
        assert (await c2.get("/api/invitations")).json() == []
        assert (await c2.delete(f"/api/invitations/{pedro['id']}")).status_code == 404
        st = (await c2.get("/api/onboarding")).json()
        assert st["run"]["channel_id"] is None and st["checks"] == []


async def test_new_org_gets_run_and_existing_orgs_do_not(meta):
    async with SessionLocal() as s:
        org = await create_org(s, "Nueva SAS", "CO", None, status="trial")
        await s.commit()
        run = await s.scalar(select(OnboardingRun).where(OnboardingRun.organization_id == org.id))
        assert run.status == "in_progress" and run.answers["company"]["name"] == "Nueva SAS"
        # Empresa que ya terminó: el asistente no se crea solo
        await s.execute(update(Organization).where(Organization.id == org.id).values(onboarding_completed_at=utcnow()))
        run.status = "completed"
        await s.commit()
    async with SessionLocal() as s:  # sesión nueva: la app no expira objetos al hacer commit
        assert await service.active_run(s, org.id, create=True) is None


async def test_template_sync_keeps_onboarding_metadata(monkeypatch):
    from app import templates
    from app.models import Channel
    from app.whatsapp import WhatsAppClient

    async def list_templates(self, waba_id):
        return [{"id": "T9", "name": "pedido_confirmado", "language": "es", "status": "APPROVED", "category": "UTILITY",
                 "components": [{"type": "BODY", "text": "Hola {{1}}, confirmamos tu pedido."}]}]

    monkeypatch.setattr(WhatsAppClient, "list_templates", list_templates)
    async with SessionLocal() as s:
        org = await create_org(s, "Sync SAS", "CO", None, status="active")
        ch = Channel(organization_id=org.id, name="WA", phone_number_id="PN-SYNC-1", waba_id="WABA-SYNC")
        s.add(ch)
        await s.flush()
        s.add(WaTemplate(organization_id=org.id, waba_id="WABA-SYNC", name="pedido_confirmado", language="es",
                         status="PENDING", components=[], source="onboarding", pack_key="retail.basico",
                         template_key="pedido_confirmado"))
        s.add(WaTemplate(organization_id=org.id, waba_id="WABA-SYNC", name="borrada_en_meta", language="es",
                         status="APPROVED", components=[]))
        await s.commit()
        out = await templates.list_templates(s, ch, refresh=True)
        assert [t["name"] for t in out] == ["pedido_confirmado"]
        row = await s.scalar(select(WaTemplate).where(WaTemplate.organization_id == org.id))
        assert (row.status, row.source, row.pack_key, row.meta_template_id) == ("APPROVED", "onboarding",
                                                                                "retail.basico", "T9")
