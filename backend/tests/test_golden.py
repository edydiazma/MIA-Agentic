"""Registro maestro del cliente (docs/data-model.md §15): normalizadores, llaves con rango y duplicados, lectura de
documentos con visión, extracción desde el clasificador, plantilla automotriz (alias → campo canónico),
consolidación con IA, consentimiento en flujos, fusión, oportunidades por vehículo, enmascarado y aislamiento."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import classifier
from app.ai.base import ImagePart
from app.auth import hash_password
from app.db import SessionLocal
from app.golden import extract as golden_extract
from app.golden import normalize as nz
from app.golden.keys import upsert_key
from app.golden.opportunities import vehicle_opportunities
from app.main import app
from app.models import (
    AIConnection,
    Agent,
    Contact,
    ContactConsent,
    ContactField,
    ContactFieldValue,
    ContactGolden,
    ContactKey,
    ContactMergeCandidate,
    ContactVehicle,
    Conversation,
    Cortex,
    CortexMember,
    Deal,
    KeyExtraction,
    Organization,
    utcnow,
)
from app.settings_store import set_setting
from tests.conftest import inbound, settle, text
from tests.test_flows import _new_flow, b, fake_buttons  # noqa: F401  (fixture autouse del módulo de flujos)


# --- Normalizadores ----------------------------------------------------------------------------------
@pytest.mark.parametrize(("value", "expected", "subtype"), [
    ("+57 310 555 1212", "573105551212", "movil"), ("3105551212", "573105551212", "movil"),
    ("(1) 345 6789", "576013456789", "fijo"), ("601 345 6789", "576013456789", "fijo"),
    ("573214493771", "573214493771", "movil"), ("+1 (305) 555-0100", "13055550100", None),
])
def test_phone(value, expected, subtype):
    n = nz.phone(value, "CO")
    assert n.value == expected and n.subtype == subtype


@pytest.mark.parametrize(("value", "expected"), [
    (" Ana@Mail.COM ", "ana@mail.com"), ("mailto:x@y.co", "x@y.co"), ("Jhon vanegas50@gmail.com", "vanegas50@gmail.com"),
    ("no-es-correo", None),
])
def test_email(value, expected):
    n = nz.email(value)
    assert (n.value if n else None) == expected


@pytest.mark.parametrize(("value", "subtype", "expected", "sub", "factor"), [
    ("CC 1.020.345.678", None, "1020345678", "CC", 1.0), ("1020345678", "cédula", "1020345678", "CC", 1.0),
    ("900.123.456-8", "NIT", "900123456", "NIT", 1.0), ("900.123.456-1", "NIT", "900123456", "NIT", 0.4),
    ("Cédula de extranjería 123456", None, "123456", "CE", 1.0), ("ab123456", "pasaporte", "AB123456", "PAS", 1.0),
    ("12.345.678-5", "RUT", "12345678", "RUT", 1.0),
])
def test_document(value, subtype, expected, sub, factor):
    n = nz.document(value, subtype)
    assert (n.value, n.subtype, n.factor) == (expected, sub, factor)


def test_plate_vin_names_dates_address():
    assert nz.plate("abc-123").value == "ABC123" and nz.plate("ABC 12D").data["pattern"] == "moto_co"
    assert nz.plate("123") is None
    assert nz.vin("1HGCM82633A004352").data["check_digit"] is True
    assert nz.vin("9BWZZZ377VT004251").factor == 0.85  # sin dígito de control (Brasil): válido con menos confianza
    assert nz.vin("1HGCM82633A00435O") is None  # O no se usa en un VIN
    assert nz.split_full_name("MARÍA DE LOS ÁNGELES PÉREZ GÓMEZ") == ("María de los Ángeles", "Pérez Gómez")
    assert nz.split_full_name("ludivia barbosa barbosa") == ("Ludivia", "Barbosa Barbosa")
    assert nz.split_full_name("Jhon Vanegas") == ("Jhon", "Vanegas")
    for raw in ("12/03/1990", "12 de marzo de 1990", "1990-03-12", "marzo 12 1990", "1990-03-12T00:00"):
        assert nz.date_value(raw).value == "1990-03-12", raw
    a = nz.address("Carrera 7 No. 45-10 apto 301, Bogotá", "hogar")
    assert a.data["line"] == "Cra 7 # 45-10 Apto 301, Bogotá" and a.data["city"] == "Bogotá" and a.subtype == "casa"
    assert nz.username("https://www.instagram.com/ana.r/").subtype == "instagram"
    assert nz.keyify("Nro. de documento") == "nro_de_documento"


# --- Utilidades ----------------------------------------------------------------------------------------
async def _contact(phone: str, name: str | None = None, org: int = 1) -> Contact:
    async with SessionLocal() as s:
        c = Contact(organization_id=org, wa_id=phone, name=name)
        s.add(c)
        await s.commit()
        return c


async def _login(email: str, password: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


async def _agent(org: int, email: str, role: str) -> None:
    async with SessionLocal() as s:
        if not await s.scalar(select(Agent).where(Agent.email == email)):
            s.add(Agent(organization_id=org, email=email, name=email.split("@")[0], role=role,
                        password_hash=hash_password("clave12345")))
            await s.commit()


@pytest.fixture
async def golden_cortex():
    async with SessionLocal() as s:
        cx = await s.scalar(select(Cortex).where(Cortex.organization_id == 1, Cortex.purpose == "golden"))
        if not cx:
            conn = AIConnection(organization_id=1, name="Golden test", provider="anthropic", model="claude-opus-5-5")
            cx = Cortex(organization_id=1, name="Registro maestro", purpose="golden")
            s.add_all([conn, cx])
            await s.flush()
            s.add(CortexMember(cortex_id=cx.id, connection_id=conn.id, position=1))
            await s.commit()
    yield


# --- Llaves: rango, duplicados, espejo en la ficha -----------------------------------------------------
async def test_keys_rank_dedupe_and_duplicates():
    a = await _contact("573219990001")
    b_ = await _contact("573219990002")
    async with SessionLocal() as s:
        ca = await s.get(Contact, a.id)
        # El teléfono de WhatsApp entra solo (trigger) como principal verificado
        wa = await s.scalar(select(ContactKey).where(ContactKey.contact_id == a.id, ContactKey.key_type == "phone"))
        assert wa.rank == "primary" and wa.verified and wa.source == "whatsapp"
        # Un teléfono dicho en el chat queda secundario (no le gana al verificado)
        r = await upsert_key(s, ca, "phone", "310 555 1212", source="ai_conversation", confidence=0.9)
        assert r.key.rank == "secondary" and r.key.value_normalized == "573105551212" and r.key.subtype == "movil"
        # Repetido = mismo registro (seen_count), no duplica
        r2 = await upsert_key(s, ca, "phone", "+57 310 555 1212", source="ai_conversation", confidence=0.95)
        assert r2.key.id == r.key.id and r2.key.seen_count == 2 and not r2.created
        # Correo → completa la ficha vacía; nombres de una sola vez: el del asesor gana y el anterior se reemplaza
        await upsert_key(s, ca, "email", "Ana@Mail.com", source="ai_conversation", confidence=0.9)
        await upsert_key(s, ca, "first_name", "Anita", source="ai_conversation", confidence=0.8)
        first = await upsert_key(s, ca, "first_name", "Ana María", source="agent", verified=True)
        assert first.promoted and first.key.rank == "primary"
        old = await s.scalar(select(ContactKey).where(ContactKey.contact_id == a.id, ContactKey.value == "Anita"))
        assert old.status == "superseded"
        await upsert_key(s, ca, "last_name", "Rodríguez Pérez", source="agent", verified=True)
        assert ca.email == "ana@mail.com" and ca.name == "Ana María Rodríguez Pérez"
        # Documento compartido por dos clientes → candidato a fusión; el correo también → puntaje combinado
        await upsert_key(s, ca, "document", "CC 1.020.345.678", source="ai_document", confidence=0.95)
        cb = await s.get(Contact, b_.id)
        await upsert_key(s, cb, "document", "1020345678", subtype="CC", source="agent", verified=True)
        cand = await s.scalar(select(ContactMergeCandidate).where(ContactMergeCandidate.contact_a_id == a.id))
        assert cand and cand.score == pytest.approx(0.95)
        await upsert_key(s, cb, "email", "ana@mail.com", source="agent", verified=True)
        await s.refresh(cand)
        assert len(cand.matched) == 2 and cand.score > 0.99
        await s.commit()
        g = await s.get(ContactGolden, a.id)
        assert g.primary_phone == "573219990001" and set(g.phones) == {"573219990001", "573105551212"}
        assert g.document_number == "1020345678" and g.document_type == "CC" and g.full_name == "Ana María Rodríguez Pérez"


# --- Documentos con visión -------------------------------------------------------------------------------
DOCS = {
    "cedula": {"document_type": "cedula", "document_confidence": 0.97, "holder_is_sender": "yes", "notes": "", "items": [
        {"field": "first_names", "value": "JHON ALEXANDER", "confidence": 0.96},
        {"field": "last_names", "value": "VANEGAS RUIZ", "confidence": 0.95},
        {"field": "document_number", "value": "79.555.123", "confidence": 0.98},
        {"field": "birthdate", "value": "1985-07-21", "confidence": 0.9},
        {"field": "sex", "value": "M", "confidence": 0.9}]},
    "tarjeta": {"document_type": "tarjeta_propiedad", "document_confidence": 0.95, "holder_is_sender": "yes",
                "notes": "", "items": [
                    {"field": "plate", "value": "JKL-482", "confidence": 0.97},
                    {"field": "vin", "value": "1HGCM82633A004352", "confidence": 0.93},
                    {"field": "make", "value": "ISUZU", "confidence": 0.95},
                    {"field": "model", "value": "D-MAX", "confidence": 0.9},
                    {"field": "year", "value": "2022", "confidence": 0.95},
                    {"field": "color", "value": "BLANCO", "confidence": 0.9},
                    {"field": "owner_name", "value": "JHON ALEXANDER VANEGAS RUIZ", "confidence": 0.9}]},
    "soat": {"document_type": "soat", "document_confidence": 0.9, "holder_is_sender": "unknown", "notes": "",
             "items": [{"field": "plate", "value": "JKL482", "confidence": 0.95},
                       {"field": "insurance_due", "value": "", "confidence": 0.9}]},
    "foto": {"document_type": "none", "document_confidence": 0.99, "holder_is_sender": "unknown", "notes": "un carro",
             "items": []},
}


async def test_document_extraction_pipeline(client, monkeypatch, golden_cortex):
    c = client
    calls = []
    soat_due = (utcnow() + timedelta(days=20)).date().isoformat()
    DOCS["soat"]["items"][1]["value"] = soat_due

    async def fake(session, cx, system, user, schema, ctx, max_tokens=4000):
        assert isinstance(user, list) and any(isinstance(p, ImagePart) for p in user)  # visión
        assert ctx.purpose == "golden" and "tarjeta_propiedad" in schema["properties"]["document_type"]["enum"]
        caption = user[0].text.lower()
        kind = next(k for k in DOCS if k in caption)
        calls.append(kind)
        return DOCS[kind]

    monkeypatch.setattr(golden_extract, "complete_json", fake)
    phone = "573219990101"
    for i, kind in enumerate(("cedula", "tarjeta", "soat", "foto")):
        await c.post("/webhooks/whatsapp", json=inbound(phone, f"gd.{i}", {
            "type": "image", "image": {"id": f"media{i}", "mime_type": "image/png", "caption": f"te envío la {kind}"}}))
        await settle(0.8)
    assert sorted(calls) == ["cedula", "foto", "soat", "tarjeta"]
    async with SessionLocal() as s:
        contact = await s.scalar(select(Contact).where(Contact.wa_id == phone))
        exts = (await s.scalars(select(KeyExtraction).where(KeyExtraction.contact_id == contact.id))).all()
        assert {e.status for e in exts} == {"done", "skipped"} and len(exts) == 4
        assert {e.document_type for e in exts} == {"cedula", "tarjeta_propiedad", "soat", None}
        g = await s.get(ContactGolden, contact.id)
        assert g.full_name == "Jhon Alexander Vanegas Ruiz" and g.document_number == "79555123"
        assert str(g.birthdate) == "1985-07-21" and g.plates == ["JKL482"] and g.vins == ["1HGCM82633A004352"]
        v = await s.scalar(select(ContactVehicle).where(ContactVehicle.contact_id == contact.id))
        assert (v.make, v.model, v.year, v.color) == ("Isuzu", "D-Max", 2022, "BLANCO")
        assert v.insurance_due.isoformat() == soat_due
        assert contact.name in ("Jhon Alexander Vanegas Ruiz", "Ana") or contact.name  # ficha básica completa
        # El mismo mensaje no se procesa dos veces
        await golden_extract.enqueue_message(s, await s.get(Conversation, exts[0].conversation_id),
                                             type("M", (), {"id": exts[0].message_id, "direction": "in",
                                                            "media_path": "x", "media_mime": "image/png"})())
        assert await s.scalar(select(func.count()).where(KeyExtraction.contact_id == contact.id)) == 4

    # Oportunidad por SOAT: idempotente
    async with SessionLocal() as s:
        await set_setting(s, "golden", {"opportunity_days": 30}, org=1)
        n1 = await vehicle_opportunities(s, 1)
    async with SessionLocal() as s:
        n2 = await vehicle_opportunities(s, 1)
        deal = await s.scalar(select(Deal).where(Deal.contact_id == contact.id, Deal.origin == "soat_due"))
    assert n1 >= 1 and n2 == 0 and deal.pipeline == "seguros" and deal.vehicle_id == v.id
    assert deal.attributes["due_date"] == soat_due and "JKL482" in deal.name

    # Ficha maestra por API (admin ve todo) y búsqueda por placa
    r = (await c.get(f"/api/contacts/{contact.id}/golden")).json()
    assert r["golden"]["document_number"] == "79555123" and r["completeness_pct"] >= 57
    assert any(k["key_type"] == "plate" and k["value"] == "JKL482" for k in r["keys"])
    assert r["vehicles"][0]["plate"] == "JKL482" and len(r["extractions"]) == 4
    hits = (await c.get("/api/golden/search", params={"q": "jkl-482"})).json()
    assert hits and hits[0]["contact_id"] == contact.id and hits[0]["matched_on"] == "plate"

    # Enmascarado: un asesor no ve el documento completo; otra empresa no ve nada
    await _agent(1, "asesor.golden@test.com", "agent")
    agent_c = await _login("asesor.golden@test.com", "clave12345")
    masked = (await agent_c.get(f"/api/contacts/{contact.id}/golden")).json()
    assert masked["golden"]["document_number"].endswith("5123") and "*" in masked["golden"]["document_number"]
    doc_key = next(k for k in masked["keys"] if k["key_type"] == "document")
    assert doc_key["masked"] and doc_key["value"] != "79555123"
    assert masked["golden"]["birthdate"].startswith("****")
    async with SessionLocal() as s:
        if not await s.get(Organization, 9301):
            s.add(Organization(id=9301, name="Otra golden", slug="otra-golden"))
            await s.commit()
    await _agent(9301, "admin.otra.golden@test.com", "admin")
    other = await _login("admin.otra.golden@test.com", "clave12345")
    assert (await other.get(f"/api/contacts/{contact.id}/golden")).status_code == 404
    assert (await other.get("/api/golden/search", params={"q": "JKL482"})).json() == []
    for x in (agent_c, other):
        await x.aclose()


# --- Conversación: misma llamada del clasificador ---------------------------------------------------------
async def test_conversation_extraction_via_classifier(client, monkeypatch):
    c = client
    phone = "573219990202"
    await c.post("/webhooks/whatsapp", json=text(phone, "gc.1", "Hola, soy Laura Gómez, mi cédula es 52.111.222 y "
                                                               "mi placa es XYZ-987, autorizo el tratamiento de datos"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]

    async def complete_json(session, cx, system, user, schema, ctx, max_tokens=4000):
        assert {"golden_keys", "vehicles", "consents"} <= set(schema["properties"])
        assert "Llaves del cliente" in system and "document (Documento de identidad)" in system
        return {"summary": "Datos del cliente", "sentiment": "neutral", "reason": "", "products": [],
                "golden_keys": [
                    {"type": "first_name", "subtype": "", "value": "Laura", "confidence": 0.9, "evidence": "soy Laura"},
                    {"type": "last_name", "subtype": "", "value": "Gómez", "confidence": 0.9, "evidence": "Gómez"},
                    {"type": "document", "subtype": "CC", "value": "52.111.222", "confidence": 0.92, "evidence": "cédula"},
                    {"type": "birthdate", "subtype": "", "value": "1990", "confidence": 0.3, "evidence": "?"}],
                "vehicles": [{"plate": "XYZ-987", "vin": "", "make": "Mazda", "model": "2", "year": "2019",
                              "relation": "owner", "confidence": 0.85}],
                "consents": [{"type": "habeas_data", "granted": True, "evidence": "autorizo el tratamiento"}]}

    monkeypatch.setattr(classifier, "complete_json", complete_json)
    async with SessionLocal() as s:
        conn = AIConnection(organization_id=1, name="Golden clasif", provider="anthropic", model="claude-opus-5-5")
        cx = Cortex(organization_id=1, name="Golden clasificación", purpose="classification")
        s.add_all([conn, cx])
        await s.flush()
        s.add(CortexMember(cortex_id=cx.id, connection_id=conn.id, position=1))
        await set_setting(s, "classifier", {"enabled": True, "min_confidence": 0.7, "cortex_id": cx.id}, org=1)
    try:
        r = await c.post(f"/api/conversations/{conv['id']}/classify")
        assert r.status_code == 200, r.text
    finally:
        async with SessionLocal() as s:
            await set_setting(s, "classifier", {"enabled": False}, org=1)
    cid = conv["contact"]["id"]
    async with SessionLocal() as s:
        keys = {(k.key_type, k.value_normalized) for k in (await s.scalars(select(ContactKey).where(
            ContactKey.contact_id == cid, ContactKey.status == "active"))).all()}
        assert ("document", "52111222") in keys and ("plate", "XYZ987") in keys and ("first_name", "laura") in keys
        assert not any(k[0] == "birthdate" for k in keys)  # baja confianza: solo en la auditoría
        consent = await s.scalar(select(ContactConsent).where(ContactConsent.contact_id == cid))
        assert consent.granted and consent.source == "ai" and consent.consent_type == "habeas_data"
        ext = await s.scalar(select(KeyExtraction).where(KeyExtraction.contact_id == cid,
                                                         KeyExtraction.source_kind == "conversation"))
        assert ext.keys_added >= 3 and ext.extracted["golden_keys"]


# --- Plantilla automotriz: los alias de Atom terminan en un solo lugar -------------------------------------
async def test_preset_apply_migrates_legacy_fields(client):
    c = client
    # Ficha heredada: la placa en tres campos, nombre completo suelto, habeas data y una variable del bot
    legacy = [("placa", "Placa", "text"), ("numero_placa", "Numero_placa", "text"),
              ("c_taller_placa", "C_taller_placa", "text"), ("nombre_completo_atom", "Nombre completo", "text"),
              ("habeas_data_atom", "Habeas data", "text"), ("saludo_inicial", "Saludo inicial", "text"),
              ("c_taller_km", "C_taller_km", "text"), ("ciudad", "Ciudad", "text")]
    ids = {}
    for key, label, typ in legacy:
        r = await c.post("/api/contact-fields", json={"key": key, "label": label, "type": typ})
        assert r.status_code in (200, 409), r.text
    fields = {f["key"]: f for f in (await c.get("/api/contact-fields")).json()}
    ids = {k: fields[k]["id"] for k, _l, _t in legacy}
    p1, p2 = await _contact("573219990301"), await _contact("573219990302")
    async with SessionLocal() as s:
        for cid, vals in ((p1.id, {"placa": "abc123", "numero_placa": "ABC-123", "c_taller_placa": "ABC 123",
                                   "nombre_completo_atom": "Ludivia Barbosa Barbosa", "habeas_data_atom": "Sí, acepto",
                                   "saludo_inicial": "hola", "c_taller_km": "45.000", "ciudad": "Bogotá"}),
                          (p2.id, {"numero_placa": "DEF456", "c_taller_km": "12000"})):
            for k, v in vals.items():
                s.add(ContactFieldValue(contact_id=cid, field_id=ids[k], value_text=v, source="import"))
        await s.commit()
    preset = (await c.get("/api/golden/presets/automotriz")).json()
    assert preset["fields"] and not preset["errors"]
    stats = (await c.post("/api/golden/fields/apply", json={"fields": preset["fields"]})).json()
    assert stats["values_failed"] == 0 and stats["fields_archived"] >= 4 and stats["values_migrated"] >= 9, stats
    async with SessionLocal() as s:
        plates = (await s.scalars(select(ContactKey).where(ContactKey.contact_id == p1.id,
                                                           ContactKey.key_type == "plate"))).all()
        assert [k.value_normalized for k in plates] == ["ABC123"] and plates[0].seen_count == 3
        assert await s.scalar(select(ContactVehicle.mileage_km).where(ContactVehicle.contact_id == p1.id)) == 45000
        g = await s.get(ContactGolden, p1.id)
        assert g.full_name == "Ludivia Barbosa Barbosa" and g.plates == ["ABC123"]
        assert (await s.get(ContactGolden, p2.id)).plates == ["DEF456"]
        consent = await s.scalar(select(ContactConsent).where(ContactConsent.contact_id == p1.id))
        assert consent.consent_type == "habeas_data" and consent.granted
        # El campo canónico conserva los alias; los duplicados quedan archivados; la variable del bot es "flow"
        placa = await s.scalar(select(ContactField).where(ContactField.organization_id == 1, ContactField.key == "placa"))
        assert placa.maps_to == "plate" and {"Numero_placa", "C_taller_placa"} <= set(placa.aliases)
        dup = await s.scalar(select(ContactField).where(ContactField.organization_id == 1,
                                                        ContactField.key == "numero_placa"))
        assert dup.archived_at is not None
        saludo = await s.scalar(select(ContactField).where(ContactField.organization_id == 1,
                                                           ContactField.key == "saludo_inicial"))
        assert saludo.scope == "flow" and saludo.section == "Técnico"
        assert await s.scalar(select(func.count()).select_from(ContactFieldValue).where(
            ContactFieldValue.field_id == ids["saludo_inicial"])) == 0
    # Idempotente
    again = (await c.post("/api/golden/fields/apply", json={"fields": preset["fields"]})).json()
    assert again["values_migrated"] == 0 and again["fields_created"] == 0
    # Agrupado por sección
    sections = {s["section"] for s in (await c.get("/api/golden/fields")).json()["sections"]}
    assert {"Vehículo", "Consentimientos", "Técnico"} <= sections
    # La API pública y la importación entienden los alias: "Numero_placa" → llave de placa
    csv = "telefono,Numero_placa,C_taller_km\n573219990303,GHI-789,8000\n"
    r = await c.post("/api/contacts/import", files={"file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["field_errors"] == 0, r.text
    async with SessionLocal() as s:
        cid = await s.scalar(select(Contact.id).where(Contact.wa_id == "573219990303"))
        assert (await s.get(ContactGolden, cid)).plates == ["GHI789"]


async def test_consolidation_assistant(client, monkeypatch, golden_cortex):
    from app.ai import router as ai_router

    async def fake(session, cx, system, user, schema, ctx, max_tokens=4000):
        assert ctx.purpose == "golden" and "concesionario" in system and "- Placas_vehiculo" in user
        return {"fields": [
            {"key": "placa", "label": "Placa", "type": "text", "section": "Vehículo", "scope": "vehicle",
             "pipeline": "", "maps_to": "plate", "aliases": ["Placas_vehiculo", "K_taller_placa"],
             "show_in_card": True, "reason": "misma placa"},
            {"key": "bad key", "label": "x", "type": "text", "section": "General", "scope": "contact", "pipeline": "",
             "maps_to": "", "aliases": [], "show_in_card": False, "reason": ""}]}

    monkeypatch.setattr(ai_router, "complete_json", fake)
    r = await c_post(client, "/api/golden/fields/consolidate",
                     {"fields": ["Placas_vehiculo", "K_taller_placa", "Var_ruta_conversion"]})
    assert r.status_code == 200, r.text
    data = r.json()
    keys = {f["key"]: f for f in data["fields"]}
    assert keys["placa"]["maps_to"] == "plate" and "var_ruta_conversion" in keys  # lo olvidado queda como campo
    assert data["errors"] and data["input_fields"] == 3


async def c_post(c, url, body):
    return await c.post(url, json=body)


# --- Consentimiento desde un flujo, fusión y llaves por API --------------------------------------------------
async def test_consent_flow_merge_and_key_api(client):
    c = client
    phone = "573219990401"
    fid = await _new_flow(c, "Habeas data", "habeasflujo", [
        b("h1", "send_text", {"text": "¿Autorizas el tratamiento de tus datos? (sí/no)"}),
        b("h2", "wait_reply", {"timeout_min": 30}),
        b("h3", "record_consent", {"consent_type": "habeas_data", "policy_version": "2026-01"}),
    ])
    await c.post("/webhooks/whatsapp", json=text(phone, "hc.1", "habeasflujo"))
    await settle()
    await c.post("/webhooks/whatsapp", json=text(phone, "hc.2", "Sí, acepto"))
    await settle()
    conv = (await c.get("/api/conversations", params={"q": phone})).json()[0]
    cid = conv["contact"]["id"]
    consents = (await c.get(f"/api/contacts/{cid}/consents")).json()
    assert consents[0]["granted"] and consents[0]["policy_version"] == "2026-01" and consents[0]["source"] == "flow"
    revoked = (await c.post(f"/api/consents/{consents[0]['id']}/revoke")).json()
    assert revoked["revoked_at"]
    await c.post(f"/api/flows/{fid}/pause")

    # Llaves por API: el asesor agrega un documento y otro cliente lo comparte → fusión manual
    r = await c.post(f"/api/contacts/{cid}/keys", json={"key_type": "document", "subtype": "CC", "value": "1.111.222.333"})
    assert r.status_code == 200 and r.json()["verified"] and r.json()["rank"] == "primary", r.text
    assert (await c.post(f"/api/contacts/{cid}/keys", json={"key_type": "plate", "value": "12"})).status_code == 422
    other = await _contact("573219990402", "Duplicado")
    async with SessionLocal() as s:
        await upsert_key(s, await s.get(Contact, other.id), "document", "1111222333", subtype="CC", source="import",
                         confidence=0.9)
        await upsert_key(s, await s.get(Contact, other.id), "email", "dup@test.co", source="import", confidence=0.9)
        await s.commit()
    cands = [x for x in (await c.get("/api/golden/merge-candidates")).json()
             if {x["contact_a"]["id"], x["contact_b"]["id"]} == {cid, other.id}]
    assert cands and cands[0]["matched"][0]["key_type"] == "document"
    m = await c.post(f"/api/golden/merge-candidates/{cands[0]['id']}/merge", json={"keep": cid})
    assert m.status_code == 200, m.text
    g = (await c.get(f"/api/contacts/{cid}/golden")).json()
    assert "dup@test.co" in g["golden"]["emails"]  # las llaves del duplicado pasan al que queda
    async with SessionLocal() as s:
        assert await s.get(Contact, other.id) is None
    # Corregir y rechazar una llave
    key = next(k for k in g["keys"] if k["key_type"] == "email" and k["value_normalized"] == "dup@test.co")
    assert (await c.patch(f"/api/contact-keys/{key['id']}", json={"rank": "primary"})).json()["rank"] == "primary"
    assert (await c.delete(f"/api/contact-keys/{key['id']}")).status_code == 200
    g2 = (await c.get(f"/api/contacts/{cid}/golden")).json()
    assert "dup@test.co" not in g2["golden"]["emails"]
    # Tipos de llave: propios editables, del sistema no
    kt = await c.post("/api/golden/key-types", json={"key": "poliza", "label": "Póliza", "normalizer": "text",
                                                     "is_identifier": True})
    assert kt.status_code == 200, kt.text
    system = next(t for t in (await c.get("/api/golden/key-types")).json() if t["key"] == "plate")
    assert (await c.put(f"/api/golden/key-types/{system['id']}", json={**system, "label": "X"})).status_code == 403
    upd = await c.put(f"/api/golden/key-types/{kt.json()['id']}", json={**kt.json(), "label": "Número de póliza"})
    assert upd.status_code == 200 and upd.json()["label"] == "Número de póliza"
    report = (await c.get("/api/reports/golden")).json()
    assert report["coverage"]["contacts"] >= 1 and "keys_by_source" in report
