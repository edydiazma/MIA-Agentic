"""Clasificación con IA sobre el modelo normalizado: etiquetas (conversation_tags), grupo, tipificación,
sugerencias (conversation_suggestions), campos tipados (contact_field_values), memoria del cliente e historial."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import classifier
from app.db import SessionLocal
from app.fields import set_custom
from app.models import (
    AIConnection,
    Channel,
    Contact,
    ContactField,
    Conversation,
    Cortex,
    CortexMember,
    ConversationSuggestion,
    ConversationTag,
    Message,
    utcnow,
)
from app.settings_store import ensure_default_ai_tags, set_setting

PHONE = "571330000001"
calls: list[dict] = []
RESULT = {
    "summary": "Cliente con reclamo por garantía del Tracker.",
    "sentiment": "negative",
    "reason": "Menciona falla en garantía.",
    "tags": [{"name": "reclamo", "confidence": 0.92}, {"name": "urgente", "confidence": 0.4}],
    "typification": "Reclamo",
    "typification_confidence": 0.9,
    "group": "Garantías",
    "group_confidence": 0.95,
    "fields": [
        {"key": "presupuesto", "value": "80.000.000", "confidence": 0.9, "evidence": "tengo 80 millones"},
        {"key": "modelo_interes", "value": "tracker", "confidence": 0.88, "evidence": "mi Tracker"},
        {"key": "ciudad", "value": "Bogotá", "confidence": 0.5, "evidence": "creo que en Bogotá"},
        {"key": "inventado", "value": "x", "confidence": 1, "evidence": ""},  # fuera de catálogo: se ignora
    ],
    "customer_memory": "- Tiene un Tracker en garantía\n- Presupuesto ~80M",
}


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    async def complete_json(session, cx, system, user, schema, ctx, max_tokens=4000):
        calls.append({"cortex": cx.name, "system": system, "user": user, "schema": schema, "purpose": ctx.purpose})
        return dict(RESULT)

    monkeypatch.setattr(classifier, "complete_json", complete_json)


async def _setup_conversation(phone: str) -> int:
    async with SessionLocal() as s:
        channel = await s.scalar(select(Channel).where(Channel.organization_id == 1).limit(1))
        contact = Contact(organization_id=1, wa_id=phone, name="Ana")
        s.add(contact)
        await s.flush()
        conv = Conversation(organization_id=1, contact_id=contact.id, channel_id=channel.id, status="bot")
        s.add(conv)
        await s.flush()
        t0 = utcnow() - timedelta(minutes=10)
        for i, (direction, sender, body) in enumerate([
            ("in", "contact", "Hola, tengo un reclamo con mi Tracker, tengo 80 millones para cambiarlo"),
            ("out", "bot", "Lo siento, ¿qué pasó?"),
            ("in", "contact", "Quiero un asesor de garantías, creo que en Bogotá"),
        ]):
            s.add(Message(organization_id=1, conversation_id=conv.id, direction=direction, sender_type=sender,
                          type="text", text=body, created_at=t0 + timedelta(minutes=i)))
        await s.commit()
        return conv.id


async def test_classifier_end_to_end(client):
    c = client
    # Las tipificaciones que usa esta prueba (otras pruebas reemplazan la lista de la empresa 1)
    await c.put("/api/settings/conversations", json={"typifications": ["Venta", "Consulta resuelta", "Reclamo"]})
    group = (await c.post("/api/groups", json={"name": "Garantías", "description": "Garantías, taller y reclamos"}))
    assert group.status_code == 200, group.text
    for f in (
        {"key": "presupuesto", "label": "Presupuesto", "type": "number", "description": "En pesos"},
        {"key": "modelo_interes", "label": "Modelo de interés", "type": "select", "options": ["Onix", "Tracker"]},
        {"key": "ciudad", "label": "Ciudad", "type": "text"},
        {"key": "cedula", "label": "Cédula", "type": "text", "ai_extract": False, "agent_editable": False},
    ):
        assert (await c.post("/api/contact-fields", json=f)).status_code == 200
    assert (await c.post("/api/contact-fields", json={"key": "Mala Clave", "label": "x"})).status_code == 422
    assert (await c.post("/api/contact-fields", json={"key": "tipo", "label": "Tipo", "type": "select"})).status_code == 422
    fields = (await c.get("/api/contact-fields")).json()
    ciudad_id = next(f["id"] for f in fields if f["key"] == "ciudad")
    assert (await c.put(f"/api/contact-fields/{ciudad_id}", json={
        "key": "ciudad", "label": "Ciudad", "type": "number"})).status_code == 422  # el tipo no cambia

    async with SessionLocal() as s:
        await ensure_default_ai_tags(s, 1)
        # Cortex propio de clasificación: se fija en la configuración (cortex_id)
        conn = AIConnection(organization_id=1, name="Clasificador test", provider="anthropic", model="claude-opus-5-5")
        cx = Cortex(organization_id=1, name="Clasificación test", purpose="classification")
        s.add_all([conn, cx])
        await s.flush()
        s.add(CortexMember(cortex_id=cx.id, connection_id=conn.id, position=1))
        # modes explícitos: otras pruebas (p. ej. el paso «IA» del onboarding) cambian los modos de la empresa 1
        from app.settings_store import DEFAULTS

        await set_setting(s, "classifier", {"enabled": True, "min_confidence": 0.7, "cortex_id": cx.id,
                                            "modes": dict(DEFAULTS["classifier"]["modes"])}, org=1)

    conv_id = await _setup_conversation(PHONE)

    # La transferencia sin grupo enruta con IA, etiqueta, llena la ficha y la memoria
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        conv.status, conv.handoff_reason, conv.handoff_at = "human", "pidió asesor", utcnow()
        await s.commit()
        await classifier.route_on_handoff(s, conv)
        await s.refresh(conv)
        assert conv.group.name == "Garantías"
        tags = (await s.execute(select(ConversationTag.source, ConversationTag.confidence)
                                .where(ConversationTag.conversation_id == conv_id))).all()
        assert len(tags) == 1 and tags[0][0] == "ai" and tags[0][1] == pytest.approx(0.92)
        sugg = (await s.scalars(select(ConversationSuggestion).where(
            ConversationSuggestion.conversation_id == conv_id, ConversationSuggestion.status == "pending"))).all()
        assert sorted((x.kind, x.target) for x in sugg) == [("field", "ciudad"), ("typification", None)]
        notes = (await s.scalars(select(Message.text).where(Message.conversation_id == conv_id,
                                                            Message.sender_type == "system"))).all()
        assert any("Garantías" in n for n in notes)

    prompt = calls[-1]
    assert prompt["purpose"] == "classification" and prompt["cortex"] == "Clasificación test"
    assert "Garantías: Garantías, taller y reclamos" in prompt["system"]
    assert "cedula" not in prompt["system"]  # sin ai_extract no se pide al modelo
    enum = prompt["schema"]["properties"]["group"]["enum"]
    assert "Garantías" in enum and enum[-1] == ""  # otras pruebas también crean grupos en la empresa 1

    r = await c.post(f"/api/conversations/{conv_id}/classify", json={"apply": False})
    conv = r.json()["conversation"]
    assert conv["tags"] == ["reclamo"] and conv["ai_sentiment"] == "negative"
    assert conv["ai_typification"] == "Reclamo"
    assert conv["ai_suggestions"]["typification"]["value"] == "Reclamo"
    assert [f["key"] for f in conv["ai_suggestions"]["fields"]] == ["ciudad"]
    contact = conv["contact"]
    assert contact["custom_fields"] == {"presupuesto": 80000000, "modelo_interes": "Tracker"}
    assert contact["memory"].startswith("- Tiene un Tracker")

    # Una persona edita la ficha: la IA ya no sobrescribe ese campo, lo sugiere
    async with SessionLocal() as s:
        contact_row = await s.get(Contact, contact["id"])
        field = await s.scalar(select(ContactField).where(ContactField.key == "presupuesto"))
        await set_custom(s, contact_row, field, 95000000, "agent", agent_id=1, conversation_id=conv_id)
        await s.commit()
    r = await c.post(f"/api/conversations/{conv_id}/classify")
    assert r.status_code == 200, r.text
    conv = r.json()["conversation"]
    assert conv["contact"]["custom_fields"]["presupuesto"] == 95000000
    assert {s["key"] for s in conv["ai_suggestions"]["fields"]} == {"presupuesto", "ciudad"}

    # Aceptar / descartar sugerencias
    r = await c.post(f"/api/conversations/{conv_id}/suggestions/accept", json={"kind": "field", "key": "ciudad"})
    assert r.json()["contact"]["custom_fields"]["ciudad"] == "Bogotá"
    r = await c.post(f"/api/conversations/{conv_id}/suggestions/dismiss", json={"kind": "field", "key": "presupuesto"})
    assert "fields" not in (r.json()["ai_suggestions"] or {})
    r = await c.post(f"/api/conversations/{conv_id}/suggestions/accept", json={"kind": "typification"})
    assert r.json()["typification"] == "Reclamo" and r.json()["ai_suggestions"] is None
    assert (await c.post(f"/api/conversations/{conv_id}/suggestions/accept",
                         json={"kind": "group"})).status_code == 404

    history = (await c.get(f"/api/contacts/{contact['id']}/history")).json()
    by_field = {(h["field_key"], h["source"]) for h in history}
    assert {("presupuesto", "ai"), ("presupuesto", "agent"), ("ciudad", "agent"), ("memory", "ai")} <= by_field
    assert next(h for h in history if h["field_key"] == "ciudad")["agent_name"] == "Administrador"

    # Etiquetas manuales y catálogo
    r = await c.put(f"/api/conversations/{conv_id}/tags", json={"tags": ["reclamo", "VIP "]})
    assert r.json()["tags"] == ["reclamo", "vip"]
    tags = {t["name"]: t for t in (await c.get("/api/conversation-tags")).json()}
    assert tags["vip"]["in_catalog"] is False and tags["reclamo"]["in_catalog"] is True
    assert tags["reclamo"]["count"] >= 1

    stats = (await c.get("/api/classifier/stats")).json()
    assert stats["by_kind"]["field"]["accepted"] >= 1 and stats["by_kind"]["field"]["acceptance_pct"] is not None

    # Prueba de configuración sin guardar (no aplica nada)
    r = await c.post("/api/classifier/test", json={"conversation_id": conv_id, "config": {"min_confidence": 0.99}})
    assert r.status_code == 200 and r.json()["applied"] is None
    assert (await c.post("/api/classifier/test", json={
        "conversation_id": conv_id, "config": {"modes": {"group": "nunca"}}})).status_code == 422

    # Borrar un campo lo archiva: los valores siguen en el historial
    presupuesto_id = next(f["id"] for f in fields if f["key"] == "presupuesto")
    assert (await c.delete(f"/api/contact-fields/{presupuesto_id}")).json() == {"ok": True}
    assert "presupuesto" not in [f["key"] for f in (await c.get("/api/contact-fields")).json()]

    # Desactivado: el análisis manual responde 409
    async with SessionLocal() as s:
        await set_setting(s, "classifier", {"enabled": False}, org=1)
    assert (await c.post(f"/api/conversations/{conv_id}/classify")).status_code == 409
