"""Hub de integraciones (§21.3): tiendas (Shopify, WooCommerce, VTEX), calendarios (Google, Outlook), Zoho / Odoo
por el marco CRM, constructor de conectores propios y exportación de datos. Toda la red es simulada."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.hub import builder, calendars, commerce, common, exports, http, odoo, zoho
from app.main import app, bootstrap
from app.models import (
    Agent,
    Appointment,
    Attribution,
    Channel,
    Contact,
    Conversation,
    DataExportRun,
    ExternalOrder,
    IntegrationConnection,
    IntegrationOutbox,
    InteractionProduct,
    Organization,
    Product,
    utcnow,
)
from app.routers.hub import verify_shopify_query

ORG, OTHER = 9730, 9731
PWD = "Hub-Secret-9730"
PHONE = "573129730001"
IDS: dict = {}


async def _setup() -> dict:
    if IDS:
        return IDS
    await bootstrap()
    async with SessionLocal() as s:
        s.add_all([Organization(id=ORG, name="Concesionario Hub", slug="org-hub-9730", onboarding_completed_at=utcnow()),
                   Organization(id=OTHER, name="Otra Hub", slug="org-hub-9731", onboarding_completed_at=utcnow())])
        await s.flush()
        admin = Agent(organization_id=ORG, email="admin@hub9730.co", name="Admin Hub", role="admin",
                      password_hash=hash_password(PWD))
        other = Agent(organization_id=OTHER, email="admin@hub9731.co", name="Admin Otra", role="admin",
                      password_hash=hash_password(PWD))
        ch = Channel(organization_id=ORG, name="WA 9730", phone_number_id="PN9730")
        s.add_all([admin, other, ch])
        await s.flush()
        contact = Contact(organization_id=ORG, wa_id=PHONE, name="Marta Ruiz", email="marta@cliente.co")
        s.add(contact)
        await s.flush()
        conv = Conversation(organization_id=ORG, contact_id=contact.id, channel_id=ch.id)
        s.add(conv)
        await s.flush()
        contact.last_conversation_id = conv.id
        attr = Attribution(organization_id=ORG, conversation_id=conv.id, contact_id=contact.id, channel="meta_ctwa",
                           matched_by="ctwa_referral", created_at=utcnow() - timedelta(days=3))
        s.add(attr)
        await s.commit()
        IDS.update(admin=admin.id, contact=contact.id, conv=conv.id, attr=attr.id, channel=ch.id)
    return IDS


async def _login(email: str = "admin@hub9730.co") -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": email, "password": PWD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


class FakeNet:
    """Servidores externos en memoria: responde por host + ruta."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.routes: dict = {}

    def on(self, method: str, url_prefix: str, fn):
        self.routes[(method, url_prefix)] = fn

    async def send(self, method, url, headers=None, json_body=None, data=None, params=None, content=None, timeout=30):
        self.calls.append((method, url, params, json_body))
        best = None
        for (m, prefix), fn in self.routes.items():
            if m == method and url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, fn)
        if not best:
            return httpx.Response(404, json={"error": f"no fake for {method} {url}"})
        out = best[1](url=url, params=params or {}, body=json_body, headers=headers or {}, data=data, content=content)
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(200, json=out)


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(http, "send", fake.send)
    s = get_settings()
    monkeypatch.setattr(s, "jobs_enabled", False)
    monkeypatch.setattr(s, "public_base_url", "https://api.test")
    monkeypatch.setattr(s, "frontend_base_url", "https://panel.test")
    return fake


# --- Constructor: JSONPath, transformaciones y validación ----------------------------------------------------------
def test_jsonpath_transforms_and_validation():
    item = {"id": 7, "customer": {"mobile": "311 222 3344", "email": " Ana@X.CO "}, "lines": [{"sku": "A"}, {"sku": "B"}],
            "make": "Chevrolet", "model": "Onix", "status": "invoiced", "date": "05/10/2026"}
    assert builder.jget(item, "$.customer.mobile") == "311 222 3344"
    assert builder.jget(item, "$.lines[*].sku") == ["A", "B"]
    assert builder.jget(item, "$.lines[1].sku") == "B"
    assert builder.jget(item, "$.nope.x") is None
    out = builder.map_item(item, {
        "phone": {"path": "$.customer.mobile", "transforms": ["phone_e164"]},
        "email": {"path": "$.customer.email", "transforms": ["trim", "lower"]},
        "name": {"value": None, "transforms": [{"concat": ["$.make", "$.model"]}]},
        "status": {"path": "$.status", "transforms": [{"map": {"invoiced": "paid"}}]},
        "placed_at": {"path": "$.date", "transforms": [{"date": "%d/%m/%Y"}]},
        "fixed": "constante"})
    assert out == {"phone": "573112223344", "email": "ana@x.co", "name": "Chevrolet Onix", "status": "paid",
                   "placed_at": "2026-10-05T00:00:00+00:00", "fixed": "constante"}
    assert builder.render("/x/{{id}}?q={{since}}", {"id": 5, "since": None}) == "/x/5?q="

    tpl = builder.template_definitions()
    assert {t["key"] for t in tpl} == {"dms_rest", "erp_inventario"}
    assert all(builder.validate(t) == [] for t in tpl)
    bad = builder.validate({"base_url": "http://x.co", "auth": {"type": "magic"},
                            "endpoints": [{"key": "a", "path": "x", "pagination": {"type": "weird"}}],
                            "mappings": {"order": {"pull": {"endpoint": "zz"}}}})
    assert len(bad) >= 5
    assert builder.validate({**tpl[0], "base_url": "https://127.0.0.1/api"})
    assert builder.validate({**tpl[0], "base_url": "https://localhost/api"})


def test_webhook_signatures():
    body = b'{"id": 1}'
    b64 = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert commerce.verify_shopify("s3cret", body, b64)
    assert not commerce.verify_shopify("s3cret", body + b" ", b64)
    assert not commerce.verify_shopify("", body, b64)
    assert commerce.verify_woo("s3cret", body, b64) and not commerce.verify_woo("otro", body, b64)
    assert commerce.verify_vtex("tok", "tok") and not commerce.verify_vtex("tok", "x") and not commerce.verify_vtex("", None)

    class D:
        webhooks = {"secret_header": "X-Sig", "signature": "hmac_sha256", "encoding": "hex"}

    hexsig = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert builder.verify_webhook(D, "k", {"x-sig": f"sha256={hexsig}"}, body)
    assert not builder.verify_webhook(D, "k", {"x-sig": "00"}, body)
    D.webhooks = {"secret_header": "X-Token", "signature": "token"}
    assert builder.verify_webhook(D, "k", {"x-token": "k"}, body) and not builder.verify_webhook(D, "k", {}, body)

    params = {"code": "c", "shop": "a.myshopify.com", "state": "s", "timestamp": "1"}
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["hmac"] = hmac.new(b"cs", msg.encode(), hashlib.sha256).hexdigest()
    assert verify_shopify_query(params, "cs") and not verify_shopify_query({**params, "shop": "b"}, "cs")
    assert http.mask("abcdefgh") == "••••••efgh"


# --- Tiendas -----------------------------------------------------------------------------------------------------
def _woo_routes(net: FakeNet, host: str, orders: list[dict]):
    net.on("GET", f"https://{host}/wp-json/wc/v3/system_status", lambda **k: {"environment": {"version": "9"}})
    net.on("GET", f"https://{host}/wp-json/wc/v3/products", lambda **k: httpx.Response(
        200, json=[{"id": 11, "sku": f"{host}-SKU1", "name": "Tapete", "price": "120000", "status": "publish",
                    "stock_quantity": 3, "stock_status": "instock"}], headers={"X-WP-TotalPages": "1"}))
    net.on("GET", f"https://{host}/wp-json/wc/v3/orders", lambda **k: httpx.Response(
        200, json=orders, headers={"X-WP-TotalPages": "1"}))


def _woo_order(oid: int, status: str, phone: str = "3129730001") -> dict:
    return {"id": oid, "number": str(oid), "status": status, "total": "240000.00", "currency": "COP",
            "date_created_gmt": (utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
            "billing": {"phone": phone, "email": "", "first_name": "Marta"},
            "line_items": [{"sku": "TAP-1", "name": "Tapete", "quantity": 2, "price": 120000}]}


async def test_multiple_woo_stores_match_attribute_and_purchase(net):
    ids = await _setup()
    _woo_routes(net, "norte.tienda.co", [_woo_order(501, "processing")])
    _woo_routes(net, "sur.tienda.co", [_woo_order(501, "pending")])
    c = await _login()
    r = await c.post("/api/hub/commerce/woocommerce", json={
        "label": "Norte", "url": "https://norte.tienda.co", "consumer_key": "ck_norte_1234",
        "consumer_secret": "cs_norte_5678", "webhook_secret": "wh-norte"})
    assert r.status_code == 200, r.text
    norte = r.json()
    assert norte["webhook_url"] == f"https://api.test/webhooks/hub/woocommerce/{norte['id']}"
    assert "consumer_key" in norte["secrets_set"] and "ck_norte" not in json.dumps(norte)
    # Misma etiqueta: conflicto; otra etiqueta: segunda tienda del mismo proveedor
    assert (await c.post("/api/hub/commerce/woocommerce", json={
        "label": "norte", "url": "https://sur.tienda.co", "consumer_key": "a", "consumer_secret": "b"})).status_code == 409
    r = await c.post("/api/hub/commerce/woocommerce", json={
        "label": "Sur", "url": "https://sur.tienda.co", "consumer_key": "ck_sur", "consumer_secret": "cs_sur"})
    assert r.status_code == 200, r.text
    sur = r.json()

    async with SessionLocal() as s:
        orders = (await s.scalars(select(ExternalOrder).where(ExternalOrder.organization_id == ORG,
                                                              ExternalOrder.external_id == "501"))).all()
        assert {o.connection_id for o in orders} == {norte["id"], sur["id"]}
        paid = next(o for o in orders if o.connection_id == norte["id"])
        assert paid.status == "paid" and paid.contact_id == ids["contact"] and paid.attribution_id == ids["attr"]
        assert float(paid.total) == 240000
        pending = next(o for o in orders if o.connection_id == sur["id"])
        assert pending.status == "pending" and pending.contact_id == ids["contact"]
        bought = (await s.scalars(select(InteractionProduct).where(
            InteractionProduct.contact_id == ids["contact"], InteractionProduct.stage == "purchased",
            InteractionProduct.name == "Tapete"))).all()
        assert len(bought) == 1 and bought[0].conversation_id == ids["conv"]
        assert await s.scalar(select(Product.id).where(Product.organization_id == ORG,
                                                       Product.sku == "norte.tienda.co-SKU1"))

    # Webhook firmado: el pedido del sur pasa a pagado → se registra la compra una sola vez
    body = json.dumps(_woo_order(501, "completed")).encode()
    sig = base64.b64encode(hmac.new(b"wh-sur", body, hashlib.sha256).digest()).decode()
    url = f"/webhooks/hub/woocommerce/{sur['id']}"
    hdr = {"X-WC-Webhook-Topic": "order.updated", "Content-Type": "application/json"}
    assert (await c.post(url, content=body, headers={**hdr, "X-WC-Webhook-Signature": sig})).status_code == 401
    await c.put(f"/api/hub/connections/{sur['id']}", json={"secrets": {"webhook_secret": "wh-sur"}})
    for _ in range(2):
        r = await c.post(url, content=body, headers={**hdr, "X-WC-Webhook-Signature": sig})
        assert r.status_code == 200, r.text
    assert (await c.post(url, content=b"webhook_id=9")).status_code == 200  # ping de creación
    async with SessionLocal() as s:
        bought = (await s.scalars(select(InteractionProduct).where(
            InteractionProduct.contact_id == ids["contact"], InteractionProduct.stage == "purchased",
            InteractionProduct.name == "Tapete"))).all()
        assert len(bought) == 1  # mismo producto en la misma conversación: una fila (interaction_products)
        sur_order = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == sur["id"]))
        assert sur_order.status == "fulfilled" and sur_order.status_raw == "completed"
    listed = (await c.get("/api/hub/orders", params={"contact_id": ids["contact"]})).json()
    assert {o["store"] for o in listed} >= {"Norte", "Sur"}
    runs = (await c.get(f"/api/hub/connections/{sur['id']}/runs")).json()
    assert {r["direction"] for r in runs} >= {"pull", "webhook"}

    # Aislamiento: la otra empresa no ve ni toca estas conexiones
    o = await _login("admin@hub9731.co")
    assert (await o.get("/api/hub/connections")).json() == []
    assert (await o.get("/api/hub/orders")).json() == []
    assert (await o.delete(f"/api/hub/connections/{sur['id']}")).status_code == 404
    assert (await o.post(f"/api/hub/connections/{sur['id']}/sync")).status_code == 404
    for x in (c, o):
        await x.aclose()


async def test_shopify_sync_and_webhook(net):
    ids = await _setup()
    shop = "hub9730.myshopify.com"
    gql = f"https://{shop}/admin/api/{get_settings().shopify_api_version}/graphql.json"

    def graphql(body, headers, **k):
        assert headers["X-Shopify-Access-Token"] == "shpat_test"
        q = body["query"]
        if "shop {" in q:
            return {"data": {"shop": {"name": "Hub", "currencyCode": "COP", "myshopifyDomain": shop}}}
        if "products(" in q:
            return {"data": {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                {"id": "gid://shopify/Product/1", "title": "Casco", "vendor": "Marca", "productType": "Accesorios",
                 "status": "ACTIVE", "updatedAt": "2026-10-01T00:00:00Z", "featuredImage": None,
                 "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9", "sku": "CASCO-M", "title": "M",
                                         "price": "350000.00", "inventoryQuantity": 4, "availableForSale": True}]}}]}}}
        return {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
            {"id": "gid://shopify/Order/77", "name": "#1077", "createdAt": utcnow().isoformat(),
             "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
             "totalPriceSet": {"shopMoney": {"amount": "350000.0", "currencyCode": "COP"}},
             "customer": {"email": "MARTA@cliente.co", "phone": None, "firstName": "Marta", "lastName": "Ruiz"},
             "lineItems": {"nodes": [{"sku": "CASCO-M", "title": "Casco", "quantity": 1,
                                      "originalUnitPriceSet": {"shopMoney": {"amount": "350000.0"}}}]}}]}}}

    net.on("POST", gql, graphql)
    c = await _login()
    r = await c.post("/api/hub/commerce/shopify", json={"label": "Shopify Hub", "shop": "hub9730",
                                                        "access_token": "shpat_test", "webhook_secret": "shp-wh"})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["settings"]["shop"] == shop
    async with SessionLocal() as s:
        order = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == conn["id"]))
        assert order.order_number == "#1077" and order.contact_id == ids["contact"]  # por correo
        p = await s.scalar(select(Product).where(Product.organization_id == ORG, Product.sku == "CASCO-M"))
        assert p.name == "Casco — M" and float(p.price) == 350000 and p.attributes["store"]["label"] == "Shopify Hub"

    payload = {"id": 78, "admin_graphql_api_id": "gid://shopify/Order/78", "name": "#1078", "financial_status": "pending",
               "total_price": "10.00", "currency": "COP", "phone": "+57 312 973 0001", "line_items": []}
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(b"shp-wh", body, hashlib.sha256).digest()).decode()
    r = await c.post(f"/webhooks/hub/shopify/{conn['id']}", content=body,
                     headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"})
    assert r.status_code == 200, r.text
    assert (await c.post(f"/webhooks/hub/shopify/{conn['id']}", content=body,
                         headers={"X-Shopify-Hmac-Sha256": "bad", "X-Shopify-Topic": "orders/create"})).status_code == 401
    assert (await c.post(f"/webhooks/hub/vtex/{conn['id']}", content=b"{}")).status_code == 404  # proveedor distinto
    async with SessionLocal() as s:
        o = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == conn["id"],
                                                       ExternalOrder.external_id == "gid://shopify/Order/78"))
        assert o.contact_id == ids["contact"] and o.status == "pending"
    assert (await c.delete(f"/api/hub/connections/{conn['id']}")).status_code == 204
    await c.aclose()


async def test_vtex_hook_fetches_order(net):
    ids = await _setup()
    base = "https://hub9730.vtexcommercestable.com.br"
    net.on("GET", f"{base}/api/oms/pvt/orders", lambda url, params, **k: (
        {"list": [], "paging": {"pages": 1, "total": 0}} if url.endswith("/orders") else
        {"orderId": "v-1", "sequence": "500", "status": "payment-approved", "value": 9990000,
         "creationDate": utcnow().isoformat(), "storePreferencesData": {"currencyCode": "COP"},
         "clientProfileData": {"email": "marta@cliente.co-123.ct.vtex.com.br", "firstName": "Marta"},
         "items": [{"refId": "R1", "name": "Llanta", "quantity": 1, "price": 9990000}]}))
    net.on("GET", f"{base}/api/catalog_system/pvt/sku/stockkeepingunitids", lambda **k: [])
    c = await _login()
    assert (await c.post("/api/hub/commerce/vtex", json={"label": "V", "account": "bad host/x", "app_key": "k",
                                                         "app_token": "t"})).status_code == 422
    r = await c.post("/api/hub/commerce/vtex", json={"label": "VTEX", "account": "hub9730", "app_key": "vtexappkey-1",
                                                     "app_token": "tok", "webhook_secret": "vh"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    url = f"/webhooks/hub/vtex/{cid}"
    assert (await c.post(url, json={"hookConfig": "ping"})).status_code == 200
    assert (await c.post(url, json={"OrderId": "v-1"}, headers={"X-Hub-Secret": "no"})).status_code == 401
    assert (await c.post(url, json={"OrderId": "v-1", "State": "payment-approved"},
                         headers={"X-Hub-Secret": "vh"})).status_code == 200
    async with SessionLocal() as s:
        o = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == cid))
        assert o.status == "paid" and float(o.total) == 99900 and o.contact_id == ids["contact"]
        assert o.customer["email"] == "marta@cliente.co"
    await c.aclose()


# --- Calendarios ----------------------------------------------------------------------------------------------------
async def test_google_calendar_oauth_push_busy_and_two_way_cancel(net, monkeypatch):
    ids = await _setup()
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "gcid")
    monkeypatch.setattr(s, "google_oauth_client_secret", "gsecret")
    events: dict[str, dict] = {}

    net.on("POST", calendars.GOOGLE_TOKEN, lambda **k: {"access_token": "g-at", "refresh_token": "g-rt", "expires_in": 3600})

    def create(url, body, **k):
        eid = f"ev{len(events) + 1}"
        events[eid] = {**body, "id": eid, "status": "confirmed"}
        return {"id": eid}

    def patch(url, body, **k):
        eid = url.rsplit("/", 1)[1]
        events[eid].update(body)
        return {"id": eid}

    def delete(url, **k):
        events[url.rsplit("/", 1)[1]]["status"] = "cancelled"
        return httpx.Response(204)

    busy_from = utcnow() + timedelta(days=2)
    net.on("POST", f"{calendars.GOOGLE_API}/calendars/primary/events", create)
    net.on("PATCH", f"{calendars.GOOGLE_API}/calendars/primary/events/", patch)
    net.on("DELETE", f"{calendars.GOOGLE_API}/calendars/primary/events/", delete)
    net.on("POST", f"{calendars.GOOGLE_API}/freeBusy", lambda body, **k: {"calendars": {"primary": {"busy": [
        {"start": busy_from.isoformat(), "end": (busy_from + timedelta(hours=1)).isoformat()}]}}})
    pulls = {"n": 0}

    def changes(url, params, **k):
        pulls["n"] += 1
        items = [{"id": eid, "status": ev["status"],
                  "extendedProperties": ev.get("extendedProperties"), "start": ev.get("start")} for eid, ev in events.items()]
        return {"items": items, "nextSyncToken": f"tok{pulls['n']}"}

    net.on("GET", f"{calendars.GOOGLE_API}/calendars/primary/events", changes)

    c = await _login()
    url = (await c.get("/api/hub/oauth/google_calendar/start")).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["https://api.test/api/hub/oauth/google_calendar/callback"]
    r = await c.get("/api/hub/oauth/google_calendar/callback", params={"code": "x", "state": q["state"][0]},
                    follow_redirects=False)
    assert "connected=1" in r.headers["location"], r.headers["location"]
    async with SessionLocal() as ses:
        conn = await ses.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == ORG, IntegrationConnection.provider == "google_calendar"))
        assert await common.secret(ses, conn, "refresh_token") == "g-rt"
        appt = Appointment(organization_id=ORG, contact_id=ids["contact"], starts_at=utcnow() + timedelta(days=1),
                           duration_min=30, title="Prueba de manejo", status="scheduled", created_by_type="agent")
        ses.add(appt)
        await ses.commit()
        appt_id, conn_id = appt.id, conn.id

    # Push: crear y luego mover
    async with SessionLocal() as ses:
        assert (await calendars.push_pending(ses, ORG))["pushed"] == 1
        a = await ses.get(Appointment, appt_id)
        assert a.external_event_id == "ev1" and events["ev1"]["extendedProperties"]["private"][calendars.MARK] == str(appt_id)
        assert "Marta Ruiz" in events["ev1"]["summary"]
        assert (await calendars.push_pending(ses, ORG))["pushed"] == 0  # sin cambios: nada que enviar
        a.starts_at = a.starts_at + timedelta(hours=2)
        a.updated_at = utcnow() + timedelta(seconds=1)
        await ses.commit()
        assert (await calendars.push_pending(ses, ORG))["pushed"] == 1
        assert ("PATCH", f"{calendars.GOOGLE_API}/calendars/primary/events/ev1") in [(m, u) for m, u, _, _ in net.calls]

    # Disponibilidad: el ocupado del calendario bloquea, nuestra propia cita no cuenta como ocupado externo
    async with SessionLocal() as ses:
        busy = await calendars.busy_intervals(ses, ORG, busy_from - timedelta(hours=1), busy_from + timedelta(hours=3))
        assert len(busy) == 1 and busy[0][0] == busy_from
        conn = await ses.get(IntegrationConnection, conn_id)
        conn.settings = {**conn.settings, "busy_blocks_slots": False}
        await ses.commit()
        assert await calendars.busy_intervals(ses, ORG, busy_from, busy_from + timedelta(hours=1)) == []

    # Dos vías: cancelado en Google → la cita queda cancelada y no se reenvía
    events["ev1"]["status"] = "cancelled"
    from app.hub.runner import sync_connection

    out = await sync_connection(conn_id)
    assert out["pull"]["updated"] == 1
    async with SessionLocal() as ses:
        a = await ses.get(Appointment, appt_id)
        assert a.status == "cancelled"
        conn = await ses.get(IntegrationConnection, conn_id)
        assert conn.settings["sync_tokens"]["primary"].startswith("tok")
    deletes = [u for m, u, _, _ in net.calls if m == "DELETE"]
    await sync_connection(conn_id)
    assert [u for m, u, _, _ in net.calls if m == "DELETE"] == deletes

    # Aislamiento + desconexión
    o = await _login("admin@hub9731.co")
    assert (await o.put(f"/api/hub/connections/{conn_id}", json={"sync_enabled": False})).status_code == 404
    assert (await c.delete(f"/api/hub/connections/{conn_id}")).status_code == 204
    for x in (c, o):
        await x.aclose()


async def test_outlook_busy_and_delta():
    calls = []

    async def fake(method, url, headers=None, json_body=None, params=None, **k):
        calls.append((method, url))
        if url.endswith("/me/calendar/getSchedule"):
            return httpx.Response(200, json={"value": [{"scheduleItems": [
                {"status": "busy", "start": {"dateTime": "2026-10-10T15:00:00"}, "end": {"dateTime": "2026-10-10T16:00:00"}},
                {"status": "free", "start": {"dateTime": "2026-10-10T17:00:00"}, "end": {"dateTime": "2026-10-10T18:00:00"}}]}]})
        if "calendarView/delta" in url:
            return httpx.Response(200, json={"value": [{"id": "e1", "@removed": {"reason": "deleted"}, "transactionId": "wa-agent-5"}],
                                             "@odata.deltaLink": "https://graph.microsoft.com/delta?token=1"})
        return httpx.Response(404)

    orig = http.send
    http.send = fake
    try:
        cl = calendars.Outlook("tok")
        busy = await cl.busy(["ventas@x.co"], datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC))
        assert busy == [(datetime(2026, 10, 10, 15, tzinfo=UTC), datetime(2026, 10, 10, 16, tzinfo=UTC))]
        items, link = await cl.changes("primary", None)
        assert items[0]["cancelled"] and items[0]["appointment_id"] == "5" and link.endswith("token=1")
    finally:
        http.send = orig


# --- Zoho / Odoo por el marco CRM -----------------------------------------------------------------------------------
async def test_zoho_oauth_and_push_through_outbox(client, monkeypatch):
    c = client
    s = get_settings()
    monkeypatch.setattr(s, "zoho_client_id", "zcid")
    monkeypatch.setattr(s, "zoho_client_secret", "zsecret")
    monkeypatch.setattr(s, "public_base_url", "https://api.test")
    monkeypatch.setattr(s, "frontend_base_url", "https://panel.test")
    store: dict[str, dict] = {}

    async def fake(method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        path = urlparse(url).path
        if path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "z-at", "refresh_token": "z-rt", "expires_in": 3600,
                                             "api_domain": "https://www.zohoapis.com"})
        if path.endswith("/org"):
            return httpx.Response(200, json={"org": [{"id": "Z-ORG"}]})
        if path.endswith("/Contacts/search"):
            return httpx.Response(204)
        if method == "GET":
            return httpx.Response(304)
        if path.endswith("/Contacts") or path.endswith("/Deals"):
            assert headers["Authorization"] == "Zoho-oauthtoken z-at"
            row = json_body["data"][0]
            rid = row.get("id") or str(len(store) + 1000)
            store[rid] = {**store.get(rid, {}), **row, "_module": path.rsplit("/", 1)[1]}
            return httpx.Response(201, json={"data": [{"code": "SUCCESS", "status": "success", "details": {"id": rid}}]})
        return httpx.Response(404)

    monkeypatch.setattr(zoho, "send", fake)
    url = (await c.get("/api/integrations/zoho/connect")).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and "ZohoCRM.modules.contacts.ALL" in q["scope"][0]
    r = await c.get("/api/integrations/zoho/callback", params={"code": "x", "state": q["state"][0]}, follow_redirects=False)
    assert "connected=1" in r.headers["location"], r.headers["location"]
    status = {x["provider"]: x for x in (await c.get("/api/integrations/crm")).json()}
    assert status["zoho"]["connected"] and status["zoho"]["external_account_id"] == "Z-ORG"
    assert not status["odoo"]["connected"]

    phone = "573129730777"
    await c.post("/webhooks/whatsapp", json={"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "PNID"}, "contacts": [{"wa_id": phone, "profile": {"name": "Zoe Zoho"}}],
        "messages": [{"from": phone, "id": "zoho.1", "type": "text", "text": {"body": "hola"}}]}}]}]})
    from tests.conftest import settle

    await settle()
    for _ in range(10):
        r = await c.post("/api/integrations/zoho/sync")
        assert r.status_code == 200, r.text
        if any(v.get("Mobile") == f"+{phone}" for v in store.values()):
            break
    contact = next(v for v in store.values() if v.get("Mobile") == f"+{phone}")
    assert contact["_module"] == "Contacts" and contact["First_Name"] == "Zoe" and contact["Last_Name"] == "Zoho"
    async with SessionLocal() as ses:
        conn = await ses.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "zoho",
                                                                    IntegrationConnection.organization_id == 1))
        assert conn.instance_url == "https://www.zohoapis.com"
        assert await ses.scalar(select(IntegrationOutbox.id).where(IntegrationOutbox.connection_id == conn.id))
    assert (await c.delete("/api/integrations/zoho")).status_code == 200


async def test_odoo_credentials_and_adapter(client, monkeypatch):
    c = client
    calls = []

    async def fake(method, url, headers=None, json_body=None, data=None, params=None, timeout=30):
        p = json_body["params"]
        calls.append((p["service"], p["method"], p["args"]))
        if p["method"] == "authenticate":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": 7 if p["args"][2] == "good-key" else False})
        if p["method"] == "version":
            return httpx.Response(200, json={"result": {"server_version": "18.0"}})
        model, method = p["args"][3], p["args"][4]
        if method == "search":
            return httpx.Response(200, json={"result": []})
        if method == "create":
            return httpx.Response(200, json={"result": 55 if model == "res.partner" else 66})
        if method == "message_post":
            return httpx.Response(200, json={"result": 99})
        return httpx.Response(200, json={"result": True})

    monkeypatch.setattr(odoo, "send", fake)
    body = {"url": "https://acme.odoo.com", "db": "acme", "login": "bot@acme.co", "api_key": "bad"}
    assert (await c.post("/api/integrations/odoo/credentials", json=body)).status_code == 400
    assert (await c.get("/api/integrations/odoo/connect")).status_code == 400
    r = await c.post("/api/integrations/odoo/credentials", json={**body, "api_key": "good-key"})
    assert r.status_code == 200, r.text
    assert r.json()["connected"] and r.json()["instance_url"] == "https://acme.odoo.com"

    from app.crm.connections import adapter_for

    async with SessionLocal() as ses:
        conn = await ses.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "odoo",
                                                                    IntegrationConnection.organization_id == 1))
        assert conn.settings["db"] == "acme" and conn.settings["server_version"] == "18.0"
        adapter = await adapter_for(ses, conn)
    assert await adapter.find_contact("x@y.co", "+57300") is None
    assert await adapter.upsert_contact(None, {"name": "Ana", "email": None}) == "55"
    assert await adapter.upsert_deal(None, {"name": "Onix"}, "55") == "66"
    assert await adapter.add_note("55", "hola\nmundo", "66") == "99"
    create_partner = next(a for s_, m, a in calls if m == "execute_kw" and a[3] == "res.partner" and a[4] == "create")
    assert create_partner[5] == [{"name": "Ana", "email": False}]
    lead = next(a for s_, m, a in calls if m == "execute_kw" and a[3] == "crm.lead" and a[4] == "create")
    assert lead[5][0]["type"] == "opportunity" and lead[5][0]["partner_id"] == 55
    assert (await c.delete("/api/integrations/odoo")).status_code == 200


# --- Conectores propios -------------------------------------------------------------------------------------------
async def test_custom_connector_test_sync_webhook_and_push(net):
    ids = await _setup()
    c = await _login()
    defs = (await c.get("/api/hub/connectors")).json()
    tpl = next(d for d in defs if d["key"] == "dms_rest")
    assert tpl["template"]
    body = {k: tpl[k] for k in ("description", "auth", "endpoints", "mappings", "webhooks")}
    body.update(name="DMS Norte", base_url="https://dms.hub9730.co/v1", is_published=True)
    body["mappings"]["contact"]["push"] = {"endpoint": "upsert_customer", "id_path": "$.data.id"}
    assert (await c.post("/api/hub/connectors", json={**body, "base_url": "https://10.0.0.5/v1"})).status_code == 422
    r = await c.post("/api/hub/connectors", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d["template"] and d["key"] == "dms_norte"
    # Plantillas: no editables
    assert (await c.put(f"/api/hub/connectors/{tpl['id']}", json=body)).status_code == 404

    seen_keys = []
    base = "https://dms.hub9730.co/v1"

    def customers(params, headers, **k):
        seen_keys.append(headers.get("X-API-Key"))
        page = int(params.get("page", 1))
        data = [{"id": f"C{page}{i}", "full_name": f" Cliente {page}{i} ", "mobile": f"31297300{page}{i}",
                 "email": f"C{page}{i}@DMS.co"} for i in range(100 if page == 1 else 1)]
        return {"data": data}

    net.on("GET", f"{base}/customers", customers)
    net.on("GET", f"{base}/inventory", lambda params, **k: {"data": [
        {"stock_id": "S1", "vin": "VIN0001", "make": "Kia", "model": "Picanto", "version": "LX", "year": 2026,
         "price": "62,990,000", "condition": "nuevo"}]})
    cursor_pages = {"n": 0}

    def sales(params, **k):
        cursor_pages["n"] += 1
        if not params.get("cursor"):
            return {"data": [{"sale_id": "F-1", "invoice_number": "FV-1", "total": 62990000, "currency": "COP",
                              "status": "invoiced", "customer": {"mobile": "3129730001"}, "date": utcnow().isoformat()}],
                    "next_cursor": "abc"}
        return {"data": [{"sale_id": "F-2", "status": "void", "customer": {"mobile": "3000000000"}}], "next_cursor": None}

    net.on("GET", f"{base}/sales", sales)
    pushed = []
    net.on("POST", f"{base}/customers", lambda body, **k: pushed.append(body) or {"data": {"id": "R-9"}})

    r = await c.post(f"/api/hub/connectors/{d['id']}/connections", json={
        "label": "DMS sede norte", "secrets": {"api_key": "dms-key-12345678", "webhook_secret": "dms-wh"},
        "settings": {"crm_push": True}})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["connector_id"] == d["id"] and set(conn["secrets_set"]) == {"api_key", "webhook_secret"}

    # Probar endpoint: primera página, máx. 5, mapeada y sin secretos en claro
    t = (await c.post(f"/api/hub/connections/{conn['id']}/test", json={"endpoint": "list_customers",
                                                                       "entity": "contact"})).json()
    assert t["ok"] and t["count"] == 5 and t["mapped"][0] == {"name": "Cliente 10", "email": "c10@dms.co",
                                                             "phone": "573129730010"}
    assert "dms-key-12345678" not in json.dumps(t)

    # Sincronizar: paginación por página (2 páginas) y por cursor; contactos, productos y pedidos
    r = await c.post(f"/api/hub/connections/{conn['id']}/sync")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["contact"]["fetched"] == 101 and res["contact"]["failed"] == 0
    assert res["product"]["created"] == 1 and res["order"]["fetched"] == 2 and cursor_pages["n"] == 2
    assert set(seen_keys) == {"dms-key-12345678"}
    async with SessionLocal() as s:
        p = await s.scalar(select(Product).where(Product.organization_id == ORG, Product.sku == "VIN0001"))
        assert p.name == "Kia Picanto LX 2026" and float(p.price) == 62990000
        f1 = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == conn["id"],
                                                        ExternalOrder.external_id == "F-1"))
        assert f1.status == "paid" and f1.contact_id == ids["contact"]
        f2 = await s.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == conn["id"],
                                                        ExternalOrder.external_id == "F-2"))
        assert f2.status == "cancelled" and f2.contact_id is None
        assert await s.scalar(select(Contact.id).where(Contact.organization_id == ORG, Contact.wa_id == "573129730010"))
        cn = await s.get(IntegrationConnection, conn["id"])
        assert set(cn.settings["cursors"]) == {"contact", "product", "order"}

    # Webhook firmado (hex)
    payload = json.dumps({"sale_id": "F-3", "status": "invoiced", "total": 5, "customer": {"mobile": "3129730001"}}).encode()
    sig = hmac.new(b"dms-wh", payload, hashlib.sha256).hexdigest()
    url = f"/webhooks/hub/custom/{conn['id']}"
    assert (await c.post(url, content=payload, headers={"X-DMS-Signature": "x"})).status_code == 401
    r = await c.post(url, content=payload, headers={"X-DMS-Signature": sig})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text

    # Push por el outbox del CRM (CustomRestAdapter)
    from app.crm.sync import enqueue_contact, sync_connection

    async with SessionLocal() as s:
        await enqueue_contact(s, ids["contact"])
        await s.commit()
    await sync_connection(conn["id"])
    assert any(b.get("phone") == f"+{PHONE}" or b.get("phone") == PHONE for b in pushed), pushed

    # El conector con conexiones no se borra
    assert (await c.delete(f"/api/hub/connectors/{d['id']}")).status_code == 409
    o = await _login("admin@hub9731.co")
    assert (await o.post(f"/api/hub/connections/{conn['id']}/test", json={"endpoint": "list_customers"})).status_code == 404
    assert d["id"] not in [x["id"] for x in (await o.get("/api/hub/connectors")).json()]
    assert (await c.delete(f"/api/hub/connections/{conn['id']}")).status_code == 204
    assert (await c.delete(f"/api/hub/connectors/{d['id']}")).status_code == 204
    for x in (c, o):
        await x.aclose()


# --- Exportación -------------------------------------------------------------------------------------------------
async def test_export_download_incremental_sensitive_and_s3(net, monkeypatch):
    ids = await _setup()
    c = await _login()
    cat = (await c.get("/api/hub/exports/datasets")).json()
    assert "contacts" in {d["key"] for d in cat["datasets"]} and "wa_id" in next(
        d for d in cat["datasets"] if d["key"] == "contacts")["sensitive_columns"]
    bad = await c.post("/api/hub/exports", json={"name": "x", "destination": "s3", "datasets": ["contacts", "secretos"]})
    assert bad.status_code == 422 and len(bad.json()["detail"]["errors"]) >= 2

    r = await c.post("/api/hub/exports", json={"name": "Descarga", "destination": "download", "format": "csv",
                                               "datasets": ["contacts", "orders"], "schedule": "manual"})
    assert r.status_code == 200, r.text
    exp = r.json()
    run = (await c.post(f"/api/hub/exports/{exp['id']}/run")).json()["run"]
    assert run["status"] == "succeeded", run
    contacts_file = next(f for f in run["files"] if f["dataset"] == "contacts")
    assert contacts_file["rows"] >= 1
    idx = run["files"].index(contacts_file)
    r = await c.get(f"/api/hub/exports/{exp['id']}/runs/{run['id']}/files/{idx}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    header = r.text.splitlines()[0].split(",")
    assert "id" in header and "wa_id" not in header and "email" not in header and PHONE not in r.text

    # Incremental: sin cambios → 0 filas; un cambio → solo esa fila
    run2 = (await c.post(f"/api/hub/exports/{exp['id']}/run")).json()["run"]
    assert run2["rows_exported"] == 0
    async with SessionLocal() as s:
        ct = await s.get(Contact, ids["contact"])
        ct.stage = "client"
        ct.updated_at = utcnow()
        await s.commit()
    run3 = (await c.post(f"/api/hub/exports/{exp['id']}/run")).json()["run"]
    assert next(f for f in run3["files"] if f["dataset"] == "contacts")["rows"] == 1

    # Sensibles: requieren el flag (y permisos); entonces sí salen
    r = await c.put(f"/api/hub/exports/{exp['id']}", json={**{k: exp[k] for k in (
        "name", "destination", "datasets", "format", "schedule", "incremental")}, "config": {},
        "include_sensitive": True})
    assert r.status_code == 200, r.text
    run4 = (await c.post(f"/api/hub/exports/{exp['id']}/run?full=true")).json()["run"]
    f = next(x for x in run4["files"] if x["dataset"] == "contacts")
    body = (await c.get(f"/api/hub/exports/{exp['id']}/runs/{run4['id']}/files/{run4['files'].index(f)}")).text
    assert "wa_id" in body.splitlines()[0] and PHONE in body

    # S3 (firma SigV4) con credenciales en Vault, nunca en la respuesta
    puts = []

    def s3_put(url, headers, content, **k):
        puts.append((url, headers, content))
        return httpx.Response(200)

    net.on("PUT", "https://lake-9730.s3.us-east-1.amazonaws.com/", s3_put)
    r = await c.post("/api/hub/exports", json={
        "name": "Lago", "destination": "s3", "format": "jsonl", "datasets": ["deals", "attributions"],
        "config": {"bucket": "lake-9730", "region": "us-east-1", "prefix": "wa"},
        "secrets": {"access_key_id": "AKIAHUB9730", "secret_access_key": "very-secret-key"}})
    assert r.status_code == 200, r.text
    lake = r.json()
    assert lake["config"]["secrets_set"] == ["access_key_id", "secret_access_key"] and "very-secret" not in r.text
    run = (await c.post(f"/api/hub/exports/{lake['id']}/run")).json()["run"]
    assert run["status"] == "succeeded", run
    assert puts and puts[0][1]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAHUB9730/")
    assert "/attributions/attributions_" in puts[-1][0]
    first = json.loads(puts[-1][2].splitlines()[0])
    assert "gclid" not in first and "utm_source" in first

    # Programación: manual nunca vence; diaria vence sin corridas previas
    async with SessionLocal() as s:
        from app.models import DataExport

        e = await s.get(DataExport, exp["id"])
        assert not exports.due(e)
        lk = await s.get(DataExport, lake["id"])
        assert exports.due(lk, utcnow() + timedelta(days=1, minutes=1))
        assert await s.scalar(select(DataExportRun.id).where(DataExportRun.export_id == exp["id"]))

    # Aislamiento
    o = await _login("admin@hub9731.co")
    assert (await o.get("/api/hub/exports")).json() == []
    assert (await o.post(f"/api/hub/exports/{exp['id']}/run")).status_code == 404
    assert (await o.get(f"/api/hub/exports/{exp['id']}/runs/{run4['id']}/files/0")).status_code == 404
    assert (await c.delete(f"/api/hub/exports/{exp['id']}")).status_code == 204
    assert (await c.delete(f"/api/hub/exports/{lake['id']}")).status_code == 204
    for x in (c, o):
        await x.aclose()


async def test_export_permission_required(net):
    await _setup()
    async with SessionLocal() as s:
        if not await s.scalar(select(Agent.id).where(Agent.email == "asesor@hub9730.co")):
            s.add(Agent(organization_id=ORG, email="asesor@hub9730.co", name="Asesor", role="agent",
                        password_hash=hash_password(PWD)))
            await s.commit()
    a = await _login("asesor@hub9730.co")
    assert (await a.get("/api/hub/exports")).status_code == 403
    assert (await a.post("/api/hub/commerce/woocommerce", json={"label": "x", "url": "https://a.co",
                                                                "consumer_key": "a", "consumer_secret": "b"})).status_code == 403
    await a.aclose()
