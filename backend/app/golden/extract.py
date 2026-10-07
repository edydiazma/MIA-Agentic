"""Extracción de llaves del registro maestro con IA (docs/data-model.md §15).

- Documentos: cada imagen o PDF que envía el cliente queda pendiente en key_extractions (una vez por mensaje). Una
  sola llamada de visión clasifica el documento (cédula, pasaporte, licencia, tarjeta de propiedad, SOAT, RTM,
  factura, RUT…) y extrae sus campos con confianza por campo; si no es un documento devuelve "none" y no se guarda
  nada más.
- Conversaciones: sin llamadas extra por mensaje. El análisis del clasificador (misma llamada) devuelve llaves,
  vehículos y consentimientos; al cerrar, si el clasificador no corrió hace poco, una única llamada de extracción.
Cada corrida queda en key_extractions (auditoría: qué se extrajo, qué se guardó, ai_call_id).
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.ai.base import DocumentPart, ImagePart, TextPart
from app.ai.router import CallContext, complete_json, resolve_cortex
from app.golden import normalize as nz
from app.golden.keys import key_types, org_country, upsert_key
from app.golden.records import CONSENT_TYPES, parse_yes_no, record_consent, upsert_vehicle
from app.models import Contact, Conversation, KeyExtraction, Message, utcnow
from app.settings_store import get_setting

log = logging.getLogger(__name__)

IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")
DOC_MIMES = ("application/pdf",)
MAX_BYTES = 15 * 1024 * 1024
DOCUMENT_TYPES = ("cedula", "cedula_extranjeria", "pasaporte", "licencia_conduccion", "tarjeta_propiedad", "soat",
                  "rtm", "factura", "rut", "otro", "none")
DOC_SUBTYPE = {"cedula": "CC", "cedula_extranjeria": "CE", "pasaporte": "PAS", "licencia_conduccion": "CC",
               "rut": "NIT"}
DOC_FIELDS = ("first_names", "last_names", "full_name", "document_type", "document_number", "birthdate", "sex",
              "address", "city", "phone", "email", "plate", "vin", "engine_number", "make", "model", "version",
              "year", "color", "fuel", "vehicle_class", "service", "insurance_due", "inspection_due", "owner_name",
              "owner_document", "company", "consent_habeas_data", "consent_terms", "consent_marketing")
RECENT_CLASSIFICATION = timedelta(minutes=15)
RELATIONS = ("owner", "driver", "interested", "previous_owner")


class ExtractionError(Exception):
    pass


# --- Encolar --------------------------------------------------------------------------------------
def is_document_media(mime: str | None) -> bool:
    return bool(mime) and (mime in IMAGE_MIMES or mime in DOC_MIMES)


async def enqueue_message(session: AsyncSession, conv: Conversation, msg: Message) -> int | None:
    """Imagen o PDF del cliente → extracción pendiente (idempotente por mensaje)."""
    if msg.direction != "in" or not msg.media_path or not is_document_media(msg.media_mime):
        return None
    if not (await get_setting(session, "golden", conv.organization_id))["extract_documents"]:
        return None
    kind = "document" if msg.media_mime in DOC_MIMES else "image"
    stmt = insert(KeyExtraction).values(
        organization_id=conv.organization_id, contact_id=conv.contact_id, conversation_id=conv.id, message_id=msg.id,
        source_kind=kind, status="pending").on_conflict_do_nothing(
        index_elements=["message_id", "source_kind"], index_where=KeyExtraction.message_id.is_not(None)
    ).returning(KeyExtraction.id)
    ext_id = (await session.execute(stmt)).scalar()
    await session.commit()
    return ext_id


# --- Documentos -----------------------------------------------------------------------------------
DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": list(DOCUMENT_TYPES)},
        "document_confidence": {"type": "number"},
        "holder_is_sender": {"type": "string", "enum": ["yes", "no", "unknown"],
                             "description": "¿El titular del documento parece ser quien escribe?"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"field": {"type": "string", "enum": list(DOC_FIELDS)},
                           "value": {"type": "string"},
                           "confidence": {"type": "number"}},
            "required": ["field", "value", "confidence"], "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["document_type", "document_confidence", "holder_is_sender", "items", "notes"],
    "additionalProperties": False,
}

DOC_SYSTEM = """Eres un lector de documentos para el registro de clientes de una empresa en Latinoamérica.
Recibes una imagen o PDF que un cliente envió por chat. Primero clasifica el documento:
cedula (cédula de ciudadanía), cedula_extranjeria, pasaporte, licencia_conduccion, tarjeta_propiedad (licencia de
tránsito del vehículo), soat (seguro obligatorio), rtm (revisión técnico-mecánica), factura, rut, otro (otro documento
con datos del cliente) o none (foto que no es un documento: un carro, un paisaje, un sticker, una captura sin datos).
Si es none, devuelve items vacío.
Luego extrae SOLO los campos que se leen en el documento, cada uno con su confianza de 0 a 1 (baja si está borroso,
cortado o tapado). No inventes ni completes datos. Formatos: fechas AAAA-MM-DD; nombres como aparecen; documento sin
puntos; placa sin guiones; VIN de 17 caracteres. En SOAT, insurance_due es la fecha de fin de vigencia; en RTM,
inspection_due es la fecha de la próxima revisión. En tarjeta de propiedad el titular va en owner_name y
owner_document. consent_* solo si el documento es un formulario firmado con esa autorización (valor "si" o "no")."""


async def process_extraction(extraction_id: int) -> KeyExtraction | None:
    """Lee el documento con IA y guarda llaves, vehículo y consentimientos (sesión propia, nunca lanza)."""
    from app.db import SessionLocal

    async with SessionLocal() as session:
        ext = await session.get(KeyExtraction, extraction_id)
        if not ext or ext.status != "pending":
            return ext
        try:
            await _run_document(session, ext)
        except Exception as e:  # noqa: BLE001 — se registra en la auditoría
            await session.rollback()
            ext = await session.get(KeyExtraction, extraction_id)
            ext.status, ext.error, ext.finished_at = "failed", f"{type(e).__name__}: {e}"[:1000], utcnow()
            log.warning("Extracción %s falló: %s", extraction_id, e)
        await session.commit()
        return ext


async def _run_document(session: AsyncSession, ext: KeyExtraction) -> None:
    msg = (await session.scalars(select(Message).where(
        Message.id == ext.message_id, Message.conversation_id == ext.conversation_id))).first()
    if not msg or not msg.media_path:
        ext.status, ext.error, ext.finished_at = "skipped", "Mensaje sin archivo", utcnow()
        return
    data = await storage.download(msg.media_path)
    if len(data) > MAX_BYTES:
        ext.status, ext.error, ext.finished_at = "skipped", "Archivo demasiado grande", utcnow()
        return
    part = (DocumentPart(data, msg.media_mime, msg.media_filename or "documento.pdf")
            if msg.media_mime in DOC_MIMES else ImagePart(data, msg.media_mime))
    caption = (msg.text or "").strip()
    user = [TextPart("Documento enviado por el cliente." + (f" Texto que lo acompaña: «{caption[:300]}»" if caption
                                                             else "")), part]
    org = ext.organization_id
    ctx = CallContext(organization_id=org, purpose="golden", conversation_id=ext.conversation_id)
    cx = await resolve_cortex(session, org, None, "golden")
    raw = await complete_json(session, cx, DOC_SYSTEM, user, DOC_SCHEMA, ctx, 2000)
    ext.ai_call_id = ctx.call_ids[-1] if ctx.call_ids else None
    doc_type = raw.get("document_type") if raw.get("document_type") in DOCUMENT_TYPES else "otro"
    ext.document_type = None if doc_type == "none" else doc_type
    ext.extracted = raw
    if doc_type == "none" or not raw.get("items"):
        ext.status, ext.finished_at = "skipped" if doc_type == "none" else "done", utcnow()
        return
    contact = await session.get(Contact, ext.contact_id)
    added, updated = await apply_document(session, contact, ext, raw)
    ext.keys_added, ext.keys_updated = added, updated
    ext.status, ext.finished_at = "done", utcnow()


def _items(raw: dict) -> dict[str, tuple[str, float]]:
    out: dict[str, tuple[str, float]] = {}
    for it in raw.get("items") or []:
        if not isinstance(it, dict) or it.get("field") not in DOC_FIELDS:
            continue
        val = str(it.get("value") or "").strip()
        if not val:
            continue
        conf = max(0.0, min(1.0, float(it.get("confidence") or 0)))
        if it["field"] not in out or conf > out[it["field"]][1]:
            out[it["field"]] = (val, conf)
    return out


async def apply_document(session: AsyncSession, contact: Contact, ext: KeyExtraction, raw: dict) -> tuple[int, int]:
    settings = await get_setting(session, "golden", contact.organization_id)
    threshold = float(settings["min_confidence"])
    types = await key_types(session, contact.organization_id)
    country = await org_country(session, contact.organization_id)
    doc_type = raw.get("document_type")
    items = _items(raw)
    holder_ok = raw.get("holder_is_sender") != "no"
    # En la tarjeta de propiedad el titular puede no ser quien escribe: sus datos personales pesan menos
    personal_factor = 0.8 if doc_type == "tarjeta_propiedad" else 1.0
    added = updated = 0
    common = {"source": "ai_document", "conversation_id": ext.conversation_id, "message_id": ext.message_id,
              "extraction_id": ext.id, "evidence": f"Documento: {doc_type}", "types": types, "country": country}

    async def put(key_type: str, value: str, conf: float, subtype: str | None = None) -> None:
        nonlocal added, updated
        if conf < threshold or key_type not in types:
            return
        res = await upsert_key(session, contact, key_type, value, subtype=subtype, confidence=conf, **common)
        if res.key is not None:
            added, updated = (added + 1, updated) if res.created else (added, updated + 1)

    if holder_ok:
        first, last = items.get("first_names"), items.get("last_names")
        full = items.get("full_name") or (items.get("owner_name") if doc_type == "tarjeta_propiedad" else None)
        if full and not (first and last):
            f, ln = nz.split_full_name(full[0])
            first = first or ((f, full[1]) if f else None)
            last = last or ((ln, full[1]) if ln else None)
        if first:
            await put("first_name", first[0], first[1] * personal_factor)
        if last:
            await put("last_name", last[0], last[1] * personal_factor)
        number = items.get("document_number") or (items.get("owner_document") if doc_type == "tarjeta_propiedad"
                                                   else None)
        if number:
            subtype = DOC_SUBTYPE.get(doc_type) or (items.get("document_type") or ("", 0))[0] or None
            await put("document", number[0], number[1] * personal_factor, subtype)
        for field_, key_type in (("birthdate", "birthdate"), ("email", "email"), ("phone", "phone"),
                                 ("company", "company")):
            if items.get(field_):
                await put(key_type, items[field_][0], items[field_][1])
        if items.get("address"):
            addr = items["address"][0]
            if items.get("city") and items["city"][0].lower() not in addr.lower():
                addr = f"{addr}, {items['city'][0]}"
            await put("address", addr, items["address"][1], "documento")

    plate, vin = items.get("plate"), items.get("vin")
    if (plate and plate[1] >= threshold) or (vin and vin[1] >= threshold):
        attrs = {k: items[k][0] for k in ("make", "model", "version", "year", "color", "fuel", "insurance_due",
                                          "inspection_due") if k in items and items[k][1] >= threshold}
        extra = {k: items[k][0] for k in ("engine_number", "vehicle_class", "service") if k in items}
        await upsert_vehicle(session, contact, plate=plate[0] if plate and plate[1] >= threshold else None,
                             vin=vin[0] if vin and vin[1] >= threshold else None, attrs=attrs, extra=extra,
                             source="ai_document", confidence=max((plate or ("", 0))[1], (vin or ("", 0))[1]),
                             conversation_id=ext.conversation_id, extraction_id=ext.id)
        updated += 1

    for field_, ctype in (("consent_habeas_data", "habeas_data"), ("consent_terms", "terms"),
                          ("consent_marketing", "marketing")):
        if items.get(field_) and items[field_][1] >= threshold:
            granted = parse_yes_no(items[field_][0])
            if granted is not None:
                await record_consent(session, contact, ctype, granted, source="ai", conversation_id=ext.conversation_id,
                                     message_id=ext.message_id, evidence=f"Documento firmado ({doc_type})")
    await session.flush()
    return added, updated


async def process_pending(limit: int = 20) -> int:
    """Extracciones pendientes (por si el proceso se reinició antes de atenderlas)."""
    from app.db import SessionLocal

    async with SessionLocal() as session:
        ids = (await session.scalars(select(KeyExtraction.id).where(
            KeyExtraction.status == "pending", KeyExtraction.source_kind.in_(("document", "image")),
            KeyExtraction.created_at < utcnow() - timedelta(seconds=30))
            .order_by(KeyExtraction.id).limit(limit))).all()
    for ext_id in ids:
        await process_extraction(ext_id)
    return len(ids)


# --- Conversaciones -------------------------------------------------------------------------------
def conversation_schema(types: dict) -> dict:
    """Propiedades que se agregan al esquema del clasificador (misma llamada)."""
    keys = [k for k, t in types.items() if t.ai_extract]
    return {
        "golden_keys": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": keys},
                           "subtype": {"type": "string",
                                       "description": "documento: CC/CE/NIT/PAS…; usuario: red; dirección: casa/"
                                                      "trabajo/envío; teléfono: movil/fijo/trabajo; \"\" si no aplica"},
                           "value": {"type": "string"},
                           "confidence": {"type": "number"},
                           "evidence": {"type": "string", "description": "Frase del cliente que lo respalda"}},
            "required": ["type", "subtype", "value", "confidence", "evidence"], "additionalProperties": False}},
        "vehicles": {"type": "array", "items": {
            "type": "object",
            "properties": {"plate": {"type": "string"}, "vin": {"type": "string"}, "make": {"type": "string"},
                           "model": {"type": "string"}, "year": {"type": "string"},
                           "relation": {"type": "string", "enum": list(RELATIONS)},
                           "confidence": {"type": "number"}},
            "required": ["plate", "vin", "make", "model", "year", "relation", "confidence"],
            "additionalProperties": False}},
        "consents": {"type": "array", "items": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": list(CONSENT_TYPES)},
                           "granted": {"type": "boolean"},
                           "evidence": {"type": "string"}},
            "required": ["type", "granted", "evidence"], "additionalProperties": False}},
    }


def conversation_instructions(types: dict) -> str:
    lines = ["## Llaves del cliente (registro maestro)",
             "Extrae los datos que identifican al cliente SOLO si él los escribió o los dictó en esta conversación "
             "(no los del asesor, la empresa ni terceros). Cada uno con su confianza y la frase de evidencia. "
             "Vehículos: solo con placa o VIN; relation=owner si es suyo, interested si lo quiere comprar. "
             "Consentimientos: solo si el cliente respondió explícitamente a una autorización (habeas data, términos, "
             "marketing). Tipos:"]
    for k, t in types.items():
        if t.ai_extract:
            lines.append(f"- {k} ({t.label})" + (f": {t.ai_hint}" if t.ai_hint else ""))
    return "\n".join(lines)


def sanitize_conversation(raw: dict, types: dict) -> dict:
    def conf(v) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    keys = [{"type": k["type"], "subtype": str(k.get("subtype") or "").strip() or None,
             "value": str(k.get("value") or "").strip()[:300], "confidence": conf(k.get("confidence")),
             "evidence": str(k.get("evidence") or "")[:300]}
            for k in raw.get("golden_keys") or []
            if isinstance(k, dict) and k.get("type") in types and str(k.get("value") or "").strip()]
    vehicles = [{"plate": str(v.get("plate") or "").strip(), "vin": str(v.get("vin") or "").strip(),
                 "make": str(v.get("make") or "").strip(), "model": str(v.get("model") or "").strip(),
                 "year": str(v.get("year") or "").strip(),
                 "relation": v.get("relation") if v.get("relation") in RELATIONS else "owner",
                 "confidence": conf(v.get("confidence"))}
                for v in raw.get("vehicles") or [] if isinstance(v, dict) and (v.get("plate") or v.get("vin"))]
    consents = [{"type": c["type"], "granted": bool(c.get("granted")), "evidence": str(c.get("evidence") or "")[:300]}
                for c in raw.get("consents") or [] if isinstance(c, dict) and c.get("type") in CONSENT_TYPES]
    return {"golden_keys": keys, "vehicles": vehicles, "consents": consents}


async def apply_conversation(session: AsyncSession, conv: Conversation, result: dict,
                             ai_call_id: int | None = None) -> KeyExtraction | None:
    """Guarda lo extraído de la conversación (desde el clasificador o la extracción al cerrar)."""
    if not (result.get("golden_keys") or result.get("vehicles") or result.get("consents")):
        return None
    org = conv.organization_id
    settings = await get_setting(session, "golden", org)
    if not settings["extract_conversations"]:
        return None
    threshold = float(settings["min_confidence"])
    types = await key_types(session, org)
    country = await org_country(session, org)
    contact = await session.get(Contact, conv.contact_id)
    ext = KeyExtraction(organization_id=org, contact_id=contact.id, conversation_id=conv.id,
                        source_kind="conversation", status="done", extracted=result, ai_call_id=ai_call_id,
                        finished_at=utcnow())
    session.add(ext)
    await session.flush()
    for k in result.get("golden_keys") or []:
        if k["confidence"] < threshold or k["type"] not in types or not types[k["type"]].ai_extract:
            continue
        res = await upsert_key(session, contact, k["type"], k["value"], subtype=k.get("subtype"),
                               source="ai_conversation", confidence=k["confidence"], conversation_id=conv.id,
                               extraction_id=ext.id, evidence=k.get("evidence"), types=types, country=country)
        if res.key is not None:
            if res.created:
                ext.keys_added += 1
            else:
                ext.keys_updated += 1
    for v in result.get("vehicles") or []:
        if v["confidence"] < threshold:
            continue
        row = await upsert_vehicle(session, contact, plate=v.get("plate") or None, vin=v.get("vin") or None,
                                   attrs={"make": v.get("make"), "model": v.get("model"), "year": v.get("year"),
                                          "relation": v.get("relation")},
                                   source="ai_conversation", confidence=v["confidence"], conversation_id=conv.id,
                                   extraction_id=ext.id)
        if row:
            ext.keys_updated += 1
    for c in result.get("consents") or []:
        await record_consent(session, contact, c["type"], c["granted"], source="ai", channel_id=conv.channel_id,
                             conversation_id=conv.id, evidence=c.get("evidence"))
    await session.flush()
    return ext


async def extract_on_close(conversation_id: int) -> KeyExtraction | None:
    """Una sola llamada al cerrar, solo si el clasificador no analizó la conversación hace poco."""
    from app.classifier import render_message
    from app.db import SessionLocal
    from app.models import Agent

    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation_id)
        if not conv:
            return None
        org = conv.organization_id
        settings = await get_setting(session, "golden", org)
        if not settings["extract_conversations"]:
            return None
        cfg = await get_setting(session, "classifier", org)
        if cfg["enabled"] and (cfg["classify_on_close"] or (
                conv.ai_classified_at and conv.ai_classified_at > utcnow() - RECENT_CLASSIFICATION)):
            return None  # el clasificador ya extrae en la misma llamada
        msgs = list(reversed((await session.scalars(
            select(Message).where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(80))).all()))
        if not any(m.direction == "in" and (m.text or m.transcript) for m in msgs):
            return None
        types = await key_types(session, org)
        names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
        transcript = "\n".join(t for t in (render_message(m, names) for m in msgs) if t)
        schema = {"type": "object", "properties": conversation_schema(types),
                  "required": ["golden_keys", "vehicles", "consents"], "additionalProperties": False}
        system = ("Extraes datos de identificación de clientes de conversaciones de chat de una empresa. "
                  "Devuelve el JSON del esquema; listas vacías si no hay datos.\n\n" + conversation_instructions(types))
        ctx = CallContext(organization_id=org, purpose="golden", conversation_id=conv.id)
        try:
            cx = await resolve_cortex(session, org, None, "golden")
            raw = await complete_json(session, cx, system, f"<conversacion>\n{transcript}\n</conversacion>", schema,
                                      ctx, 2000)
        except Exception as e:  # noqa: BLE001 — la extracción nunca rompe el cierre
            log.warning("Extracción al cerrar %s falló: %s", conversation_id, e)
            return None
        ext = await apply_conversation(session, conv, sanitize_conversation(raw, types),
                                       ctx.call_ids[-1] if ctx.call_ids else None)
        await session.commit()
        return ext

