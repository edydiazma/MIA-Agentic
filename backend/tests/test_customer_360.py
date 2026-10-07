"""Cliente 360: identidad de WhatsApp (teléfono + BSUID + @usuario), métricas de interacción, lista de clientes,
productos por interacción (asesor, IA, pedidos, flujos, API) y reportes de BI. docs/data-model.md §14"""

import csv
import io

import pytest
from sqlalchemy import select, text

from app import classifier
from app.db import SessionLocal
from app.identity import IdentityError, is_bsuid, require_phone_for_template, wa_address
from app.models import (
    AIConnection,
    Channel,
    Contact,
    ContactChange,
    ContactIdentity,
    ContactTag,
    Conversation,
    Cortex,
    CortexMember,
    InteractionProduct,
    Organization,
    Product,
)
from app.settings_store import set_setting
from app.whatsapp import WhatsAppClient
from tests.conftest import WA, settle
from tests.test_flows import _new_flow, b, fake_buttons  # noqa: F401  (fixture autouse del módulo de flujos)

BSUID = "CO.13491208655302741918"
BSUID2 = "CO.99991208655302741000"


def wa_webhook(msg_id: str, body: dict, phone: str | None = None, bsuid: str | None = None,
               username: str | None = None, name: str = "Cliente", field: str = "messages") -> dict:
    """Webhook de WhatsApp con el formato de 2026: user_id siempre, wa_id/from solo a veces."""
    contact = {"profile": {"name": name}}
    if username:
        contact["profile"]["username"] = username
    if phone:
        contact["wa_id"] = phone
    if bsuid:
        contact["user_id"] = bsuid
    msg = {"id": msg_id, "timestamp": "1760000000", **body}
    if phone:
        msg["from"] = phone
    if bsuid:
        msg["from_user_id"] = bsuid
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": field, "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
        "contacts": [contact], "messages": [msg]}}]}]}


def text_body(t: str) -> dict:
    return {"type": "text", "text": {"body": t}}


async def _contact_by(**kw) -> Contact | None:
    async with SessionLocal() as s:
        q = select(Contact).where(Contact.organization_id == 1)
        for k, v in kw.items():
            q = q.where(getattr(Contact, k) == v)
        return (await s.scalars(q)).first()


def test_bsuid_helpers():
    assert is_bsuid(BSUID) and is_bsuid("US.ENT.11815799212886844830")
    assert not is_bsuid("573001234567") and not is_bsuid("co.123") and not is_bsuid(None)
    # El cliente de WhatsApp manda el BSUID en `recipient` y el teléfono en `to`
    assert WhatsAppClient.address({"to": BSUID, "type": "text"}) == {
        "type": "text", "recipient_type": "individual", "recipient": BSUID}
    assert WhatsAppClient.address({"to": "573001234567"}) == {"to": "573001234567"}
    c = Contact(organization_id=1, wa_bsuid=BSUID)
    assert wa_address(c) == BSUID
    with pytest.raises(IdentityError):
        require_phone_for_template(c, "AUTHENTICATION")
    require_phone_for_template(c, "MARKETING")  # marketing/utilidad sí se envían al BSUID


async def test_whatsapp_identity_bsuid_phone_link_merge_and_update(client):
    c = client
    # 1) Solo usuario de WhatsApp (sin teléfono): contacto con BSUID y @usuario; la respuesta va al BSUID
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.1", text_body("hola"), bsuid=BSUID,
                                                       username="ana.ruiz", name="Ana Ruiz"))
    await settle()
    ana = await _contact_by(wa_bsuid=BSUID)
    assert ana and ana.wa_id is None and ana.wa_username == "ana.ruiz" and ana.name == "Ana Ruiz"
    assert WA.sent[-1][0] == BSUID  # el bot respondió por BSUID (recipient)
    async with SessionLocal() as s:
        ident = (await s.scalars(select(ContactIdentity).where(ContactIdentity.contact_id == ana.id))).one()
        assert (ident.bsuid, ident.phone, ident.external_id, ident.username) == (BSUID, None, BSUID, "ana.ruiz")

    # 2) Luego llega con teléfono + mismo BSUID: se vincula al mismo contacto
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.2", text_body("otra vez"), phone="573110000001",
                                                       bsuid=BSUID, username="ana.ruiz"))
    await settle()
    same = await _contact_by(wa_bsuid=BSUID)
    assert same.id == ana.id and same.wa_id == "573110000001"
    assert WA.sent[-1][0] == "573110000001"  # con teléfono, Meta usa `to`

    # 3) Duplicado: un contacto importado por teléfono y otro creado por BSUID → al llegar ambos se fusionan
    imported = await c.post("/api/contacts", json={"wa_id": "573110000002", "name": "Pedro (CSV)", "tags": ["c360-importado"]})
    assert imported.status_code == 200, imported.text
    imported_id = imported.json()["id"]
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.3", text_body("hola soy pedro"), bsuid=BSUID2,
                                                       username="pedrog"))
    await settle()
    by_bsuid = await _contact_by(wa_bsuid=BSUID2)
    assert by_bsuid.id != imported_id
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.4", text_body("sigo"), phone="573110000002",
                                                       bsuid=BSUID2, username="pedrog"))
    await settle()
    merged = await _contact_by(wa_bsuid=BSUID2)
    assert merged.id == min(imported_id, by_bsuid.id) and merged.wa_id == "573110000002"
    assert await _contact_by(id=max(imported_id, by_bsuid.id)) is None
    async with SessionLocal() as s:
        convs = (await s.scalars(select(Conversation).where(Conversation.contact_id == merged.id))).all()
        assert len(convs) >= 1
        tags = (await s.scalars(select(ContactTag).where(ContactTag.contact_id == merged.id))).all()
        assert tags  # la etiqueta del contacto importado se conserva
        assert (await s.get(Contact, merged.id)).messages_in == 2  # métricas recalculadas tras la fusión
        changes = (await s.scalars(select(ContactChange.field_key).where(ContactChange.contact_id == merged.id))).all()
        assert "merge" in changes

    # 4) user_id_update: el cliente cambió de número → BSUID nuevo, queda en el historial
    new_bsuid = "CO.55551208655302741999"
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "user_id_update", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"},
            "contacts": [{"profile": {"name": "Ana"}, "wa_id": "573110000001"}],
            "user_id_update": [{"wa_id": "573110000001", "detail": "User id updated",
                                "user_id": {"previous": BSUID, "current": new_bsuid}, "timestamp": "1760000001"}]}}]}]}
    assert (await c.post("/webhooks/whatsapp", json=payload)).status_code == 200
    await settle()
    updated = await _contact_by(id=ana.id)
    assert updated.wa_bsuid == new_bsuid
    async with SessionLocal() as s:
        ch = (await s.scalars(select(ContactChange).where(ContactChange.contact_id == ana.id,
                                                          ContactChange.field_key == "wa_bsuid"))).all()
        assert any(x.old_value == BSUID and x.new_value == new_bsuid and x.source == "whatsapp" for x in ch)


async def test_client_list_metrics_filters_export_and_reports(client):
    c = client
    phone = "573120000001"
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.l1", text_body("hola, info del Kayak"), phone=phone,
                                                       bsuid="CO.77771208655302741000", username="laura"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Consulta resuelta"]})
    assert (await c.post(f"/api/conversations/{conv['id']}/assign")).status_code == 200
    r = await c.post(f"/api/conversations/{conv['id']}/close", json={"typification": "Venta"})
    assert r.status_code == 200, r.text

    page = (await c.get("/api/contacts", params={"q": "laura", "sort": "last_interaction_at", "order": "desc"})).json()
    assert page["total"] >= 1
    item = next(i for i in page["items"] if i["wa_id"] == phone)
    assert item["wa_username"] == "laura" and item["wa_bsuid"] == "CO.77771208655302741000"
    assert item["messages_in"] == 1 and item["messages_out"] >= 1 and item["conversations_count"] == 1
    assert item["first_interaction_at"] and item["last_interaction_at"] and item["first_inbound_at"]
    assert item["lifetime_days"] is not None and item["days_since_last_interaction"] is not None
    assert item["channel_providers"] == ["whatsapp_cloud"]
    assert item["channels"] and item["channels"][0]["provider"] == "whatsapp_cloud"
    assert item["last_typification"]["name"] == "Venta"
    for key in ("first_source_channel", "first_source_label", "last_source_at"):
        assert key in item

    # Filtros: canal (tipo e id), tipificación, inactividad, fechas, búsqueda por BSUID
    async with SessionLocal() as s:
        channel_id = await s.scalar(select(Channel.id).where(Channel.phone_number_id == "PNID"))
    ids_by = {}
    for params in ({"channels": "whatsapp_cloud"}, {"channel_ids": str(channel_id)},
                   {"typification_id": item["last_typification"]["id"]}, {"q": "CO.7777"},
                   {"created_from": item["created_at"][:10]}, {"last_interaction_from": item["last_interaction_at"][:10]}):
        res = (await c.get("/api/contacts", params={**params, "limit": 500})).json()
        ids_by[str(params)] = {i["id"] for i in res["items"]}
        assert item["id"] in ids_by[str(params)], params
    assert item["id"] not in {i["id"] for i in (await c.get("/api/contacts", params={
        "channels": "instagram"})).json()["items"]}
    assert item["id"] not in {i["id"] for i in (await c.get("/api/contacts", params={
        "inactive_days_gte": 30, "limit": 500})).json()["items"]}
    assert (await c.get("/api/contacts", params={"sort": "nope"})).status_code == 422
    for sort in ("lifetime_days", "days_since_last_interaction", "products_count", "first_source_at"):
        assert (await c.get("/api/contacts", params={"sort": sort, "order": "asc"})).status_code == 200

    cols = (await c.get("/api/contacts/columns")).json()
    keys = {x["key"] for x in cols}
    assert {"name", "wa_id", "tags", "channels", "last_agent", "last_typification", "created_at", "updated_at",
            "first_interaction_at", "last_interaction_at", "days_since_last_interaction", "last_product_name",
            "first_source_label"} <= keys
    assert {x["group"] for x in cols} >= {"Básico", "Interacción", "Productos", "Fuente"}

    r = await c.get("/api/contacts/export.csv", params={"q": phone, "columns": "name,wa_id,wa_username,channels,"
                                                                              "last_typification,messages_in"})
    assert r.status_code == 200 and r.text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0] == ["Nombre completo", "Teléfono", "Usuario de WhatsApp", "Canales", "Tipificación",
                       "Mensajes recibidos"]
    assert rows[1][1] == phone and rows[1][2] == "laura" and rows[1][4] == "Venta" and rows[1][5] == "1"

    rep = (await c.get("/api/reports/customers")).json()
    assert rep["totals"]["contacts"] >= 1 and rep["totals"]["active"] >= 1 and rep["new_series"]
    assert sum(b["count"] for b in rep["recency_buckets"]) == rep["totals"]["contacts"]
    assert any(x["provider"] == "whatsapp_cloud" for x in rep["by_channel"])
    typs = (await c.get("/api/typifications")).json()
    assert any(t["name"] == "Venta" and "is_success" in t and "active" in t for t in typs)


async def test_products_per_interaction(client, monkeypatch):
    c = client
    async with SessionLocal() as s:
        s.add_all([Product(organization_id=1, sku="KZ-01", name="Kayak Zephyr", price=80000000, currency="COP"),
                   Product(organization_id=1, sku="BN-02", name="Bicicleta Nimbus", price=120000000,
                           currency="COP")])
        await s.commit()
    phone = "573130000001"
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.p1", text_body("me interesa el kayak zephyr y un seguro"),
                                                       phone=phone, bsuid="CO.88881208655302741000"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]
    contact_id = conv["contact"]["id"]

    # Asesor: por catálogo (dedupe → 200 la segunda vez) y por código externo
    found = (await c.get("/api/products/search", params={"q": "kayak"})).json()
    kayak = next(p for p in found if p["sku"] == "KZ-01")
    r1 = await c.post(f"/api/conversations/{conv['id']}/products", json={"product_id": kayak["id"], "stage": "quoted"})
    r2 = await c.post(f"/api/conversations/{conv['id']}/products", json={"product_id": kayak["id"], "stage": "quoted"})
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    assert r1.json()["product"]["sku"] == "KZ-01" and r1.json()["unit_price"] == 80000000
    ext = await c.post(f"/api/conversations/{conv['id']}/products",
                       json={"external_ref": "ERP-SEG-01", "name": "Seguro todo riesgo", "stage": "interested"})
    assert ext.status_code == 201 and ext.json()["product"] is None and ext.json()["external_ref"] == "ERP-SEG-01"
    assert (await c.post(f"/api/conversations/{conv['id']}/products", json={"stage": "zzz", "name": "x"})
            ).status_code == 422
    patched = await c.patch(f"/api/interaction-products/{ext.json()['id']}", json={"stage": "purchased",
                                                                                   "quantity": 1})
    assert patched.status_code == 200 and patched.json()["stage"] == "purchased"

    # IA: la misma llamada de clasificación detecta productos (≥ 0.6) y los enlaza al catálogo
    async def complete_json(session, cx, system, user, schema, ctx, max_tokens=4000):
        assert "products" in schema["properties"] and "KZ-01: Kayak Zephyr" in system
        return {"summary": "Interés en Kayak", "sentiment": "positive", "reason": "", "products": [
            {"name": "Kayak Zephyr", "catalog_sku_or_null": "KZ-01", "stage": "interested", "confidence": 0.9},
            {"name": "Moto", "catalog_sku_or_null": "", "stage": "mentioned", "confidence": 0.3}]}

    monkeypatch.setattr(classifier, "complete_json", complete_json)
    async with SessionLocal() as s:
        conn = AIConnection(organization_id=1, name="C360 test", provider="anthropic", model="claude-opus-5-5")
        cx = Cortex(organization_id=1, name="C360 clasificación", purpose="classification")
        s.add_all([conn, cx])
        await s.flush()
        s.add(CortexMember(cortex_id=cx.id, connection_id=conn.id, position=1))
        await set_setting(s, "classifier", {"enabled": True, "min_confidence": 0.7, "cortex_id": cx.id}, org=1)
    r = await c.post(f"/api/conversations/{conv['id']}/classify")
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        await set_setting(s, "classifier", {"enabled": False}, org=1)
    items = (await c.get(f"/api/conversations/{conv['id']}/products")).json()
    by = {(i["name"], i["stage"]): i for i in items}
    assert ("Kayak Zephyr", "interested") in by and by[("Kayak Zephyr", "interested")]["source"] == "ai"
    assert ("Moto", "mentioned") not in by  # confianza baja

    # Pedido de WhatsApp: comprado, enlazado por SKU (y SKU desconocido como referencia externa)
    order = {"type": "order", "order": {"catalog_id": "CAT1", "product_items": [
        {"product_retailer_id": "BN-02", "quantity": 1, "item_price": 118000000, "currency": "COP"},
        {"product_retailer_id": "ACC-99", "quantity": 2, "item_price": 50000, "currency": "COP"}]}}
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.p2", order, phone=phone,
                                                       bsuid="CO.88881208655302741000"))
    await settle()
    items = (await c.get(f"/api/contacts/{contact_id}/products")).json()
    purchased = {(i["product"] or {}).get("sku") or i["external_ref"]: i for i in items if i["stage"] == "purchased"}
    assert purchased["BN-02"]["source"] == "whatsapp_order" and purchased["BN-02"]["unit_price"] == 118000000
    assert purchased["ACC-99"]["product"] is None and purchased["ACC-99"]["quantity"] == 2

    # Flujo: bloque register_product
    fid = await _new_flow(c, "Registra producto", "quieronimbus", [
        b("rp1", "register_product", {"product": "BN-02", "stage": "interested", "external_ref": "CRM-77"})])
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.p3", text_body("quieronimbus"), phone=phone,
                                                       bsuid="CO.88881208655302741000"))
    await settle()
    items = (await c.get(f"/api/contacts/{contact_id}/products")).json()
    assert any(i["source"] == "flow" and (i["product"] or {}).get("sku") == "BN-02" and i["stage"] == "interested"
               for i in items)
    await c.post(f"/api/flows/{fid}/pause")

    # Métricas en la ficha y borrado (el trigger descuenta)
    contact = next(i for i in (await c.get("/api/contacts", params={"q": phone})).json()["items"])
    n = contact["products_count"]
    assert n == len(items) and contact["last_product_name"]
    assert (await c.delete(f"/api/interaction-products/{ext.json()['id']}")).status_code == 200
    contact = next(i for i in (await c.get("/api/contacts", params={"q": phone})).json()["items"])
    assert contact["products_count"] == n - 1
    with_products = (await c.get("/api/contacts", params={"has_products": "true", "limit": 500})).json()["items"]
    assert contact_id in {i["id"] for i in with_products}
    assert contact_id in {i["id"] for i in (await c.get("/api/contacts", params={"product": "Kayak",
                                                                               "limit": 500})).json()["items"]}

    # Reporte de productos
    async with SessionLocal() as s:
        await s.execute(text("select reporting.refresh_range(1, current_date - 1, current_date + 1)"))
        await s.commit()
    rep = (await c.get("/api/reports/products")).json()
    top = {t["name"]: t for t in rep["top"]}
    assert top["Bicicleta Nimbus"]["purchased"] >= 1 and rep["totals"]["purchased"] >= 2
    assert rep["totals"]["contacts"] >= 1 and rep["series"]


async def test_public_api_and_isolation(client):
    c = client
    key = (await c.post("/api/api-keys", json={"name": "c360", "scopes": ["contacts:read", "contacts:write",
                                                                          "messages:send"]})).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    # Contacto solo-BSUID: se le escribe por contact_id y por bsuid (recipient)
    await c.post("/webhooks/whatsapp", json=wa_webhook("c360.a1", text_body("hola"), bsuid="CO.66661208655302741000",
                                                       username="solo.user"))
    await settle()
    cid = (await _contact_by(wa_bsuid="CO.66661208655302741000")).id
    r = await c.post("/v1/messages", headers=h, json={"contact_id": cid, "text": "Hola por API"})
    assert r.status_code == 201, r.text
    assert WA.sent[-1] == ("CO.66661208655302741000", "Hola por API")
    r = await c.post("/v1/messages", headers={**h, "Idempotency-Key": "c360-b"},
                     json={"bsuid": "CO.66661208655302741000", "text": "Otra"})
    assert r.status_code == 201 and r.json()["contact_id"] == cid
    assert (await c.post("/v1/messages", headers=h, json={"bsuid": "malo", "text": "x"})).status_code == 422
    got = (await c.get(f"/v1/contacts/{cid}", headers=h)).json()
    assert got["whatsapp_bsuid"] == "CO.66661208655302741000" and got["whatsapp_username"] == "solo.user"
    added = await c.post(f"/v1/contacts/{cid}/products", headers=h, json={"external_ref": "SKU-X", "name": "Producto X",
                                                                         "stage": "quoted"})
    assert added.status_code == 201, added.text
    assert (await c.get(f"/v1/contacts/{cid}/products", headers=h)).json()["data"][0]["external_ref"] == "SKU-X"

    # Aislamiento: otra empresa no ve los productos ni puede registrar en la conversación
    async with SessionLocal() as s:
        if not await s.get(Organization, 9360):
            s.add(Organization(id=9360, name="Org 360", slug="org-360", timezone="America/Bogota"))
            await s.flush()
        row = (await s.scalars(select(InteractionProduct).where(InteractionProduct.contact_id == cid))).first()
        conv_id = row.conversation_id
        other = Contact(organization_id=9360, wa_bsuid="CO.66661208655302741000")  # mismo BSUID, otra empresa: OK
        s.add(other)
        await s.commit()
    from app.auth import create_token  # noqa: PLC0415
    from app.models import Agent

    async with SessionLocal() as s:
        agent = Agent(organization_id=9360, email="a360@test.com", name="A360", role="admin")
        s.add(agent)
        await s.commit()
        token = create_token(agent)
    oh = {"Authorization": f"Bearer {token}"}
    assert (await c.get(f"/api/conversations/{conv_id}/products", headers=oh)).status_code == 404
    assert (await c.post(f"/api/conversations/{conv_id}/products", headers=oh,
                         json={"name": "x", "stage": "quoted"})).status_code == 404
    assert (await c.get(f"/api/contacts/{cid}/products", headers=oh)).status_code == 404
    assert cid not in {i["id"] for i in (await c.get("/api/contacts", headers=oh)).json()["items"]}
