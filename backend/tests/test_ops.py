"""Routers de operación sobre el modelo Supabase: Vault, Storage, tags/campos normalizados, tipificaciones,
transferencias y aislamiento entre organizaciones."""

import httpx
from sqlalchemy import select, text

from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import Agent, Alert, Channel, Organization, Typification
from tests.conftest import settle
from tests.conftest import text as wa_text


async def test_webhook_secret_in_vault(client):
    c = client
    w = (await c.post("/api/outbound-webhooks", json={"name": "CRM", "url": "https://crm.example.com/h"})).json()
    assert len(w["secret"]) == 48  # solo al crear
    listed = (await c.get("/api/outbound-webhooks")).json()
    row = next(x for x in listed if x["id"] == w["id"])
    assert row["secret"] == "" and row["has_secret"] is True
    assert (await c.get(f"/api/outbound-webhooks/{w['id']}/secret")).json()["secret"] == w["secret"]
    rotated = (await c.post(f"/api/outbound-webhooks/{w['id']}/rotate-secret")).json()
    assert rotated["secret"] != w["secret"]
    async with SessionLocal() as s:  # el secreto vive en Vault, no en la tabla
        stored = await s.scalar(text("select count(*) from vault.secrets where name = :n"),
                                {"n": f"outbound_webhook:{w['id']}"})
    assert stored == 1
    await c.delete(f"/api/outbound-webhooks/{w['id']}")


async def test_resources_storage_and_channel_token(client):
    c = client
    up = await c.post("/api/resources", files={"file": ("ficha.pdf", b"%PDF-1.4 ficha", "application/pdf")})
    assert up.status_code == 200, up.text
    rid = up.json()["id"]
    f = await c.get(f"/api/resources/{rid}/file")
    assert f.content == b"%PDF-1.4 ficha" and f.headers["content-type"] == "application/pdf"
    await c.delete(f"/api/resources/{rid}")
    assert (await c.get(f"/api/resources/{rid}/file")).status_code == 404

    ch = (await c.post("/api/channels", json={"name": "Ventas", "phone_number_id": "PN-OPS",
                                               "access_token": "EAAG-secreto"})).json()
    assert ch["has_own_token"] is True and "access_token" not in ch
    async with SessionLocal() as s:
        sid = (await s.get(Channel, ch["id"])).access_token_secret_id
        assert await s.scalar(text("select decrypted_secret from vault.decrypted_secrets where id = cast(:i as uuid)"),
                              {"i": sid}) == "EAAG-secreto"


async def test_typifications_sync_alerts_quick_replies(client):
    c = client
    cfg = (await c.get("/api/settings/conversations")).json()
    names = cfg["typifications"] + ["Garantía"]
    r = await c.put("/api/settings/conversations", json={"typifications": [n for n in names if n != "Spam"]})
    assert "Garantía" in r.json()["typifications"] and "Spam" not in r.json()["typifications"]
    async with SessionLocal() as s:  # nunca se borra: queda inactiva para los reportes
        spam = await s.scalar(select(Typification).where(Typification.organization_id == 1, Typification.name == "Spam"))
        assert spam.is_active is False
    await c.put("/api/settings/conversations", json={"typifications": names})
    assert (await c.put("/api/settings/conversations", json={"typifications": [" "]})).status_code == 422

    async with SessionLocal() as s:
        s.add(Alert(organization_id=1, layer="contact", title="Prueba ops"))
        await s.commit()
    alert = next(a for a in (await c.get("/api/alerts")).json() if a["title"] == "Prueba ops")
    assert alert["resolved"] is False
    await c.post(f"/api/alerts/{alert['id']}/resolve")
    resolved = (await c.get("/api/alerts", params={"resolved": True})).json()
    assert any(a["id"] == alert["id"] and a["resolved"] and a["resolved_at"] for a in resolved)

    assert (await c.post("/api/quick-replies", json={"shortcut": "/hola mundo", "text": "x"})).status_code == 422
    assert (await c.post("/api/quick-replies", json={"shortcut": "/zz_ops", "text": "Hola!"})).json()["shortcut"] == "zz_ops"


async def test_contacts_tags_fields_import_and_transfer(client):
    c = client
    ct = (await c.post("/api/contacts", json={"wa_id": "+57 300 555 0001", "name": "Ops", "tags": ["Ops_VIP", "ops_x"]})).json()
    assert ct["wa_id"] == "573005550001" and ct["tags"] == ["ops_vip", "ops_x"]
    await c.post("/api/contact-fields", json={"key": "presupuesto_ops", "label": "Presupuesto ops", "type": "number"})
    r = await c.put(f"/api/contacts/{ct['id']}", json={"tags": ["ops_vip"], "custom_fields": {"presupuesto_ops": "$ 1.500.000"},
                                                      "stage": "prospect"})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["ops_vip"] and r.json()["custom_fields"] == {"presupuesto_ops": 1500000}
    assert (await c.put(f"/api/contacts/{ct['id']}", json={"stage": "otro"})).status_code == 422
    tags = {t["tag"]: t["count"] for t in (await c.get("/api/contacts/tags")).json()}
    assert tags["ops_vip"] == 1
    assert (await c.get("/api/contacts", params={"tag": "ops_vip", "q": "Ops"})).json()["total"] == 1

    csv_file = "telefono,nombre,Presupuesto ops\n573005550002,Leo,2.000\n573005550003,Mia,abc\n"
    imp = (await c.post("/api/contacts/import", data={"tags": "feria"},
                        files={"file": ("c.csv", csv_file.encode(), "text/csv")})).json()
    assert imp["created"] == 2 and imp["field_errors"] == 1 and imp["custom_columns"] == ["Presupuesto ops"]
    leo = (await c.get("/api/contacts", params={"q": "573005550002"})).json()["items"][0]
    assert leo["custom_fields"] == {"presupuesto_ops": 2000} and leo["tags"] == ["feria"]

    # Transferencia a grupo con nota (nota interna en el chat)
    await c.post("/webhooks/whatsapp", json=wa_text("573005550009", "ops.1", "quiero un asesor"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": "573005550009"})).json()[0]
    g = (await c.post("/api/groups", json={"name": "Posventa ops"})).json()
    t = (await c.post(f"/api/conversations/{conv['id']}/transfer", json={"group_id": g["id"], "note": "garantía"})).json()
    assert t["group"]["name"] == "Posventa ops" and t["assigned_agent"] is None
    msgs = (await c.get(f"/api/conversations/{conv['id']}/messages")).json()
    assert any(m["sender_type"] == "system" and "garantía" in m["text"] for m in msgs)
    assert len((await c.get("/api/conversations", params={"group_id": g["id"]})).json()) == 1


async def test_cross_organization_isolation(client):
    async with SessionLocal() as s:
        if not await s.get(Organization, 8):
            s.add(Organization(id=8, name="Otra", slug="otra-ops"))
            await s.flush()
            s.add(Agent(organization_id=8, email="admin8@test.com", name="Admin 8", role="admin",
                        password_hash=hash_password("secret88")))
            await s.commit()
    any_conv = (await client.get("/api/conversations")).json()[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as other:
        tok = (await other.post("/api/auth/login", json={"email": "admin8@test.com", "password": "secret88"})).json()
        other.headers["Authorization"] = f"Bearer {tok['access_token']}"
        assert (await other.get("/api/conversations")).json() == []
        assert (await other.get(f"/api/conversations/{any_conv['id']}")).status_code == 404
        assert (await other.get(f"/api/contacts/{any_conv['contact']['id']}")).status_code == 404
        assert (await other.get("/api/contacts")).json()["total"] == 0
        assert (await other.get("/api/reports/general")).json()["totals"]["new_conversations"] == 0
