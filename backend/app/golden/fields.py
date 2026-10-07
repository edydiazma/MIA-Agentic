"""Organización de campos del cliente (docs/data-model.md §15).

Una ficha con decenas de campos sueltos y duplicados (Placa / Numero_placa / C_taller_placa…) se ordena así:
- `aliases`: nombres anteriores (Atom, bots, CRM, importaciones). Flujos, importaciones, la API pública y el CRM
  resuelven cualquier alias al campo canónico (`resolve_field`).
- `maps_to`: el valor no queda suelto, va a su lugar canónico (`write_value`):
    <tipo de llave> (document, plate, email…) → registro maestro · full_name → nombres + apellidos ·
    document.subtype → tipo del documento · vehicle.<atributo> → vehículo del cliente · consent.<tipo> →
    consentimiento · deal.<atributo> → oportunidad abierta de la línea (pipeline) · contact.<columna> → ficha.
- `scope = flow`: variable de trabajo del bot (no se guarda en el cliente).
"""

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fields import FIELD_TYPES, KEY_RE, coerce, set_custom, set_native
from app.golden import normalize as nz
from app.golden.keys import key_types, upsert_key
from app.golden.records import VEHICLE_FIELDS, parse_yes_no, record_consent, upsert_vehicle
from app.models import Contact, ContactField, ContactFieldValue, ContactKey, ContactVehicle, Deal, utcnow

SCOPES = ("contact", "deal", "vehicle", "appointment", "flow")
PIPELINES = ("nuevos", "usados", "taller", "repuestos_accesorios", "pqr", "eventos", "seguros")
SECTIONS = ("Identidad", "Contacto", "Vehículo", "Nuevos", "Usados", "Taller", "Repuestos y accesorios", "PQR",
            "Eventos", "Consentimientos", "Marketing", "Atención", "Técnico", "General")
CONTACT_COLUMNS = ("name", "email", "stage", "notes")
DEAL_COLUMNS = ("amount", "name", "currency", "interest_level", "expected_close")
PRESETS_DIR = Path(__file__).with_name("presets")
# Origen de cada escritura → fuente en cada tabla
KEY_SOURCE = {"agent": "agent", "ai": "ai_conversation", "import": "import", "api": "api", "crm": "crm",
              "flow": "flow"}
CONSENT_SOURCE = {"agent": "agent", "ai": "ai", "import": "import", "api": "api", "crm": "import", "flow": "flow"}
VEHICLE_SOURCE = {"agent": "agent", "ai": "ai_conversation", "import": "import", "api": "api", "crm": "crm",
                  "flow": "flow"}
DEAL_SOURCE = {"agent": "agent", "ai": "ai", "import": "import", "api": "api", "crm": "crm", "flow": "flow"}
MAPS_RE = re.compile(r"^(full_name|[a-z][a-z0-9_]{0,49}|(vehicle|consent|deal|contact|document)\.[a-z][a-z0-9_]{0,49})$")


def validate_organization(scope: str | None, pipeline: str | None, maps_to: str | None) -> None:
    if scope is not None and scope not in SCOPES:
        raise ValueError(f"Alcance inválido: {', '.join(SCOPES)}")
    if scope == "deal" and not pipeline:
        raise ValueError("Un campo de oportunidad necesita la línea de negocio (pipeline)")
    if pipeline is not None and not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", pipeline):
        raise ValueError("Línea de negocio inválida (minúsculas y _)")
    if maps_to:
        if not MAPS_RE.match(maps_to):
            raise ValueError("Destino inválido (llave, full_name, vehicle.x, consent.x, deal.x o contact.x)")
        prefix, dot, attr = maps_to.partition(".")
        if not dot:
            return  # llave del registro maestro o full_name
        # vehicle.<otro atributo> va a contact_vehicles.attributes; deal.<otro> a deals.attributes
        if prefix == "contact" and attr not in CONTACT_COLUMNS:
            raise ValueError(f"contact.* admite: {', '.join(CONTACT_COLUMNS)}")
        if prefix == "document" and attr != "subtype":
            raise ValueError("document.* solo admite document.subtype")


# --- Resolución de nombres -----------------------------------------------------------------------
async def field_index(session: AsyncSession, org: int) -> dict[str, ContactField]:
    """clave, etiqueta y alias (normalizados) → campo activo."""
    rows = (await session.scalars(select(ContactField).where(
        ContactField.organization_id == org, ContactField.archived_at.is_(None))
        .order_by(ContactField.position, ContactField.id))).all()
    idx: dict[str, ContactField] = {}
    for f in rows:  # alias primero, después etiqueta y clave (la clave exacta siempre gana)
        for a in f.aliases or []:
            idx.setdefault(nz.keyify(a), f)
    for f in rows:
        idx[nz.keyify(f.label)] = idx.get(nz.keyify(f.label)) or f
    for f in rows:
        idx[f.key] = f
    return idx


async def resolve_field(session: AsyncSession, org: int, name: str,
                        index: dict[str, ContactField] | None = None) -> ContactField | None:
    index = index if index is not None else await field_index(session, org)
    return index.get(name) or index.get(nz.keyify(name))


# --- Escritura al destino canónico ---------------------------------------------------------------
async def write_value(session: AsyncSession, contact: Contact, field: ContactField, raw, source: str, *,
                      agent_id: int | None = None, conversation_id: int | None = None) -> str:
    """Guarda el valor donde corresponde. Devuelve a dónde fue: key, vehicle, consent, deal, contact, custom, flow,
    skipped. Lanza ValueError si el valor no es válido para el campo."""
    if field.scope == "flow":
        return "flow"
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "skipped"
    target = field.maps_to
    if not target:
        if field.scope == "vehicle":
            vehicle = await _latest_vehicle(session, contact.id)
            if vehicle:
                vehicle.attributes = {**(vehicle.attributes or {}), field.key: str(raw).strip()[:200]}
                vehicle.updated_at = utcnow()
                return "vehicle"
        if field.scope == "deal" and field.pipeline:
            deal = await _open_deal(session, contact, field.pipeline, source, agent_id, conversation_id)
            deal.attributes = {**(deal.attributes or {}), field.key: coerce(field, raw)}
            deal.updated_at = utcnow()
            return "deal"
        await set_custom(session, contact, field, coerce(field, raw), source, agent_id=agent_id,
                         conversation_id=conversation_id)
        return "custom"
    prefix, dot, attr = target.partition(".")
    if not dot:
        prefix = ""
    if target == "full_name":
        first, last = nz.split_full_name(str(raw))
        ksrc = KEY_SOURCE.get(source, "agent")
        for kt, val in (("first_name", first), ("last_name", last)):
            if val:
                await upsert_key(session, contact, kt, val, source=ksrc, confidence=0.9 if ksrc != "agent" else None,
                                 verified=ksrc == "agent", agent_id=agent_id, conversation_id=conversation_id)
        return "key"
    if prefix == "contact":
        value = raw.strip() if isinstance(raw, str) else raw
        set_native(session, contact, attr, value, source if source != "crm" else "crm", agent_id=agent_id,
                   conversation_id=conversation_id)
        return "contact"
    if prefix == "document" and attr == "subtype":
        doc = (await session.scalars(select(ContactKey).where(
            ContactKey.contact_id == contact.id, ContactKey.key_type == "document", ContactKey.status == "active")
            .order_by(ContactKey.rank == "primary", ContactKey.last_seen_at.desc()))).first()
        code = nz.doc_type(str(raw))
        if doc and code and not doc.subtype:
            doc.subtype = code
            return "key"
        await set_custom(session, contact, field, coerce(field, raw), source, agent_id=agent_id,
                         conversation_id=conversation_id)
        return "custom"
    if target in ("plate", "vin"):  # la placa o el VIN crean / completan el vehículo (y su llave)
        prefix, attr = "vehicle", target
    if prefix == "vehicle":
        if attr in ("plate", "vin"):
            row = await upsert_vehicle(session, contact, **{attr: str(raw)}, source=VEHICLE_SOURCE.get(source, "agent"),
                                       conversation_id=conversation_id)
            if not row:
                raise ValueError(f"«{field.label}» no es una {'placa' if attr == 'plate' else 'VIN'} válida")
            return "vehicle"
        vehicle = await _latest_vehicle(session, contact.id)
        if not vehicle:  # sin placa ni VIN todavía: el valor queda en la ficha hasta que llegue el vehículo
            await set_custom(session, contact, field, coerce(field, raw), source, agent_id=agent_id,
                             conversation_id=conversation_id)
            return "custom"
        if attr in VEHICLE_FIELDS:
            from app.golden.records import _clean_vehicle

            clean = _clean_vehicle({attr: raw})
            if attr in clean:
                setattr(vehicle, attr, clean[attr])
        else:
            vehicle.attributes = {**(vehicle.attributes or {}), attr: str(raw).strip()[:200]}
        vehicle.updated_at = utcnow()
        return "vehicle"
    if prefix == "consent":
        granted = parse_yes_no(raw)
        if granted is None:
            raise ValueError(f"«{field.label}» debe ser sí o no")
        await record_consent(session, contact, attr, granted, source=CONSENT_SOURCE.get(source, "agent"),
                             conversation_id=conversation_id, agent_id=agent_id, evidence=str(raw)[:300])
        return "consent"
    if prefix == "deal":
        deal = await _open_deal(session, contact, field.pipeline or "general", source, agent_id, conversation_id)
        if attr == "amount":
            n = re.sub(r"[^\d.,]", "", str(raw)).replace(".", "").replace(",", ".")
            deal.amount = float(n) if n else deal.amount
            if field.currency:
                deal.currency = field.currency
        elif attr == "interest_level":
            v = nz.fold(str(raw))
            deal.interest_level = ("hot" if v in ("hot", "caliente", "si", "alto", "1", "true") else
                                   "cold" if v in ("cold", "frio", "bajo", "no", "0", "false") else "warm")
        elif attr == "name":
            deal.name = str(raw).strip()[:200]
        else:
            deal.attributes = {**(deal.attributes or {}), attr: str(raw).strip()[:300]}
        deal.updated_at = utcnow()
        return "deal"
    # Llave del registro maestro
    types = await key_types(session, contact.organization_id)
    if target not in types:
        raise ValueError(f"Destino desconocido: {target}")
    ksrc = KEY_SOURCE.get(source, "agent")
    res = await upsert_key(session, contact, target, raw, source=ksrc, confidence=0.9 if ksrc != "agent" else None,
                           verified=ksrc == "agent", agent_id=agent_id, conversation_id=conversation_id, types=types)
    if res.key is None:
        raise ValueError(f"«{field.label}» no tiene un formato válido")
    return "key"


async def _latest_vehicle(session: AsyncSession, contact_id: int) -> ContactVehicle | None:
    return (await session.scalars(select(ContactVehicle).where(
        ContactVehicle.contact_id == contact_id, ContactVehicle.status == "active")
        .order_by(ContactVehicle.updated_at.desc()))).first()


async def _open_deal(session: AsyncSession, contact: Contact, pipeline: str, source: str, agent_id: int | None,
                     conversation_id: int | None) -> Deal:
    deal = (await session.scalars(select(Deal).where(
        Deal.contact_id == contact.id, Deal.pipeline == pipeline, Deal.status == "open")
        .order_by(Deal.updated_at.desc()))).first()
    if deal:
        return deal
    deal = Deal(organization_id=contact.organization_id, contact_id=contact.id, conversation_id=conversation_id,
                owner_agent_id=agent_id or contact.last_agent_id,
                name=f"{pipeline.replace('_', ' ').capitalize()} · {contact.name or contact.wa_id or contact.id}",
                pipeline=pipeline, stage="new", status="open", source=DEAL_SOURCE.get(source, "agent"), attributes={})
    session.add(deal)
    await session.flush()
    return deal


# --- Presets y consolidación ------------------------------------------------------------------------
def list_presets() -> list[dict]:
    out = []
    for p in sorted(PRESETS_DIR.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        out.append({"key": p.stem, "name": data.get("name"), "description": data.get("description"),
                    "fields": len(data.get("fields", [])),
                    "aliases": sum(len(f.get("aliases", [])) for f in data.get("fields", []))})
    return out


def load_preset(key: str) -> dict:
    p = PRESETS_DIR / f"{key}.json"
    if not re.fullmatch(r"[a-z0-9_]+", key) or not p.exists():
        raise KeyError(key)
    return json.loads(p.read_text(encoding="utf-8"))


def validate_proposal(fields: list[dict]) -> tuple[list[dict], list[str]]:
    """Limpia una propuesta (preset o IA). Devuelve (campos válidos, errores)."""
    out, errors, seen = [], [], set()
    for f in fields or []:
        key = str(f.get("key") or "").strip()
        if not KEY_RE.match(key):
            errors.append(f"Clave inválida: {key or '(vacía)'}")
            continue
        if key in seen:
            errors.append(f"Clave repetida: {key}")
            continue
        typ = f.get("type") or "text"
        if typ not in FIELD_TYPES:
            typ = "text"
        scope = f.get("scope") or "contact"
        pipeline = f.get("pipeline") or None
        maps_to = f.get("maps_to") or None
        try:
            validate_organization(scope, pipeline, maps_to)
        except ValueError as e:
            errors.append(f"{key}: {e}")
            continue
        seen.add(key)
        out.append({"key": key, "label": str(f.get("label") or key)[:80], "type": typ,
                    "section": str(f.get("section") or "General")[:40], "scope": scope, "pipeline": pipeline,
                    "maps_to": maps_to, "aliases": sorted({str(a).strip() for a in f.get("aliases") or []
                                                           if str(a).strip()}),
                    "show_in_card": bool(f.get("show_in_card")), "options": f.get("options") or None,
                    "currency": f.get("currency") or None, "description": f.get("description") or None})
    return out, errors


async def apply_proposal(session: AsyncSession, org: int, fields: list[dict], agent_id: int | None = None) -> dict:
    """Crea/actualiza los campos canónicos, migra los valores de los alias a su destino y archiva duplicados.
    Idempotente: correrlo dos veces no duplica nada."""
    clean, errors = validate_proposal(fields)
    stats = {"fields_created": 0, "fields_updated": 0, "fields_archived": 0, "values_migrated": 0,
             "values_failed": 0, "errors": errors, "targets": {}}
    existing = (await session.scalars(select(ContactField).where(ContactField.organization_id == org))).all()
    by_name: dict[str, list[ContactField]] = {}
    for f in existing:
        for name in {f.key, nz.keyify(f.label), *(nz.keyify(a) for a in f.aliases or [])}:
            by_name.setdefault(name, []).append(f)
    contacts_cache: dict[int, Contact] = {}

    for spec in clean:
        names = {spec["key"], nz.keyify(spec["label"]), *(nz.keyify(a) for a in spec["aliases"])}
        matches = list({f.id: f for n in names for f in by_name.get(n, [])}.values())
        canonical = next((f for f in matches if f.key == spec["key"]), None)
        if canonical is None:
            canonical = next((f for f in matches if f.archived_at is None and f.type == spec["type"]), None)
            if canonical is not None and canonical.key != spec["key"]:
                # el campo existente con otra clave queda como canónico (sus valores no se mueven)
                spec["aliases"] = sorted({*spec["aliases"], canonical.key})
        if canonical is None:
            canonical = ContactField(organization_id=org, key=spec["key"], label=spec["label"], type=spec["type"],
                                     options=spec["options"], description=spec["description"])
            session.add(canonical)
            stats["fields_created"] += 1
        else:
            stats["fields_updated"] += 1
        canonical.label, canonical.section, canonical.scope = spec["label"], spec["section"], spec["scope"]
        canonical.pipeline, canonical.maps_to, canonical.show_in_card = spec["pipeline"], spec["maps_to"], \
            spec["show_in_card"]
        canonical.aliases = sorted({*(canonical.aliases or []), *spec["aliases"]})
        canonical.currency = spec["currency"] or canonical.currency
        if canonical.type != spec["type"] and canonical.id is None:
            canonical.type = spec["type"]
        canonical.archived_at = None
        await session.flush()

        # Migración de valores: duplicados → destino canónico; el canónico con maps_to → registro maestro
        sources = [f for f in matches if f.id != canonical.id]
        if canonical.maps_to or canonical.scope in ("vehicle", "deal", "flow"):
            sources.append(canonical)
        for src in sources:
            values = (await session.scalars(select(ContactFieldValue).where(
                ContactFieldValue.field_id == src.id))).all()
            for v in values:
                value = v.value
                contact = contacts_cache.get(v.contact_id) or await session.get(Contact, v.contact_id)
                contacts_cache[v.contact_id] = contact
                plain = not canonical.maps_to and canonical.scope == "contact"
                if plain and src.id != canonical.id and await session.get(ContactFieldValue, (contact.id, canonical.id)):
                    continue  # el campo canónico ya tiene valor: el del duplicado queda archivado, no lo pisa
                try:
                    where = await write_value(session, contact, canonical, value, "import", agent_id=agent_id)
                except ValueError:
                    stats["values_failed"] += 1
                    continue
                stats["targets"][where] = stats["targets"].get(where, 0) + 1
                if src.id != canonical.id or where != "custom":  # el valor ya vive en su destino canónico
                    await session.delete(v)
                stats["values_migrated"] += 1
            if src.id != canonical.id and src.archived_at is None:
                src.archived_at = utcnow()
                stats["fields_archived"] += 1
            await session.flush()
    return stats


# --- Asistente de consolidación con IA ----------------------------------------------------------------
CONSOLIDATE_SYSTEM = """Ordenas la ficha de clientes de una empresa que tiene muchos campos sueltos y duplicados
(nombres heredados de otra plataforma, de bots y de importaciones). Agrupa todos los nombres que significan lo mismo
en UN campo canónico (los nombres originales van en aliases; cada nombre de entrada debe quedar en exactamente un
campo). Para cada campo canónico define:
- key: snake_case en español, único; label legible; type: text | long_text | number | currency | date | datetime |
  select | boolean | email | phone.
- section: Identidad, Contacto, Vehículo, Nuevos, Usados, Taller, Repuestos y accesorios, PQR, Eventos,
  Consentimientos, Marketing, Atención, Técnico o General.
- scope: contact (dato del cliente), deal (calificación de una oportunidad de una línea de negocio; pipeline
  obligatorio), vehicle (dato del vehículo del cliente), appointment (dato de una cita) o flow (variable de trabajo
  del bot: saludos, rutas, ids, horas ISO, comentarios automáticos; no se guarda en el cliente).
- maps_to (opcional): una llave del registro maestro (first_name, last_name, document, email, phone, address,
  birthdate, plate, vin, username, company, occupation), full_name (nombre completo), document.subtype,
  vehicle.<atributo> (make, model, year, mileage_km, color…), consent.<tipo> (habeas_data, terms, marketing),
  deal.<atributo> (amount, interest_level o un atributo libre) o contact.<name|email|stage|notes>.
- show_in_card: true solo para lo esencial de la ficha resumen (5 a 8 campos).
Usa el ejemplo de concesionario como guía de estilo."""


def proposal_schema() -> dict:
    return {"type": "object", "properties": {"fields": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "label": {"type": "string"},
                       "type": {"type": "string", "enum": list(FIELD_TYPES)},
                       "section": {"type": "string", "enum": list(SECTIONS)},
                       "scope": {"type": "string", "enum": list(SCOPES)},
                       "pipeline": {"type": "string", "description": "línea de negocio si scope=deal; \"\" si no"},
                       "maps_to": {"type": "string", "description": "destino canónico o \"\""},
                       "aliases": {"type": "array", "items": {"type": "string"}},
                       "show_in_card": {"type": "boolean"},
                       "reason": {"type": "string"}},
        "required": ["key", "label", "type", "section", "scope", "pipeline", "maps_to", "aliases", "show_in_card",
                     "reason"],
        "additionalProperties": False}}},
        "required": ["fields"], "additionalProperties": False}


async def consolidate(session: AsyncSession, org: int, names: list[str]) -> dict:
    """Propuesta de campos canónicos para una lista de nombres de campos (una llamada al LLM)."""
    from app.ai.router import CallContext, complete_json, resolve_cortex

    names = [n.strip() for n in names if n and n.strip()][:400]
    if not names:
        raise ValueError("No hay campos para consolidar")
    example = load_preset("automotriz")
    sample = [{k: f.get(k) for k in ("key", "label", "type", "section", "scope", "pipeline", "maps_to", "aliases",
                                     "show_in_card")} for f in example["fields"][:30]]
    system = CONSOLIDATE_SYSTEM + "\n\nEjemplo (concesionario):\n" + json.dumps(sample, ensure_ascii=False)
    ctx = CallContext(organization_id=org, purpose="golden")
    cx = await resolve_cortex(session, org, None, "golden")
    raw = await complete_json(session, cx, system, "Campos actuales:\n" + "\n".join(f"- {n}" for n in names),
                              proposal_schema(), ctx, 8000)
    fields_ = raw.get("fields") or []
    for f in fields_:
        f["pipeline"] = f.get("pipeline") or None
        f["maps_to"] = f.get("maps_to") or None
    clean, errors = validate_proposal(fields_)
    # Todo nombre de entrada debe quedar en algún campo: los que la IA olvidó quedan como su propio campo
    covered = {nz.keyify(a) for f in clean for a in (*f["aliases"], f["key"], f["label"])}
    for n in names:
        if nz.keyify(n) not in covered:
            key = (nz.keyify(n) or "campo")[:50]
            key = key if KEY_RE.match(key) else f"c_{key}"[:50]
            if key not in {f["key"] for f in clean}:
                clean.append({"key": key, "label": n[:80], "type": "text", "section": "General", "scope": "contact",
                              "pipeline": None, "maps_to": None, "aliases": [n], "show_in_card": False,
                              "options": None, "currency": None, "description": None})
                covered.add(nz.keyify(n))
    return {"fields": clean, "errors": errors, "input_fields": len(names), "canonical_fields": len(clean),
            "ai_call_id": ctx.call_ids[-1] if ctx.call_ids else None}
