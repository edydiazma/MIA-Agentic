"""Registro maestro del cliente: llaves, vehículos, consentimientos, duplicados y organización de campos (§15).

Los datos sensibles (documento, fecha de nacimiento, dirección) se enmascaran para asesores salvo que la empresa lo
permita (Configuraciones → golden.agents_see_sensitive); administradores y supervisores ven el valor completo.
"""

import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.golden import normalize as nz
from app.golden.fields import (
    PIPELINES,
    SCOPES,
    SECTIONS,
    apply_proposal,
    consolidate,
    list_presets,
    load_preset,
    validate_proposal,
)
from app.golden.keys import GoldenError, key_types, mask, refresh_golden, upsert_key
from app.golden.records import VEHICLE_FIELDS, RecordError, record_consent, upsert_vehicle
from app.models import (
    Agent,
    Contact,
    ContactConsent,
    ContactField,
    ContactGolden,
    ContactKey,
    ContactMergeCandidate,
    ContactVehicle,
    GoldenKeyType,
    KeyExtraction,
    utcnow,
)
from app.settings_store import get_setting

router = APIRouter(prefix="/api", tags=["golden"])
NORMALIZERS = ("phone", "email", "username", "document", "plate", "vin", "name", "date", "address", "text")


async def _contact(session: AsyncSession, contact_id: int, agent: Agent) -> Contact:
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Cliente no encontrado")
    return c


async def _sees(session: AsyncSession, agent: Agent) -> bool:
    """Permiso «data.sensitive.view» (rol) o el ajuste de la empresa que lo abre a todos los asesores."""
    from app.permissions import has_permission

    if await has_permission(session, agent, "data.sensitive.view"):
        return True
    settings = await get_setting(session, "golden", agent.organization_id)
    return bool(settings.get("agents_see_sensitive"))


# --- Tipos de llave ----------------------------------------------------------------------------------
class KeyTypeIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    label: str = Field(min_length=1, max_length=80)
    normalizer: str = "text"
    is_identifier: bool = False
    is_sensitive: bool = False
    multi: bool = True
    ai_extract: bool = True
    ai_hint: str | None = None
    position: int = 200
    archived: bool = False


def _kt_out(t: GoldenKeyType) -> dict:
    return {"id": t.id, "key": t.key, "label": t.label, "normalizer": t.normalizer, "is_identifier": t.is_identifier,
            "is_sensitive": t.is_sensitive, "multi": t.multi, "ai_extract": t.ai_extract, "ai_hint": t.ai_hint,
            "position": t.position, "system": t.organization_id is None, "archived": t.archived_at is not None}


@router.get("/golden/key-types")
async def list_key_types(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return [_kt_out(t) for t in (await key_types(session, agent.organization_id, include_archived=True)).values()]


@router.post("/golden/key-types")
async def create_key_type(body: KeyTypeIn, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    if body.normalizer not in NORMALIZERS:
        raise HTTPException(422, f"Normalizador inválido: {', '.join(NORMALIZERS)}")
    types = await key_types(session, agent.organization_id, include_archived=True)
    if body.key in types:
        raise HTTPException(409, "Ya existe una llave con esa clave")
    t = GoldenKeyType(organization_id=agent.organization_id, **body.model_dump(exclude={"archived"}))
    session.add(t)
    await session.commit()
    return _kt_out(t)


@router.put("/golden/key-types/{type_id}")
async def update_key_type(type_id: int, body: KeyTypeIn, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    t = await session.get(GoldenKeyType, type_id)
    if not t or (t.organization_id not in (None, agent.organization_id)):
        raise HTTPException(404, "Tipo de llave no encontrado")
    if t.organization_id is None:
        raise HTTPException(403, "Las llaves del sistema no se pueden modificar")
    if body.normalizer not in NORMALIZERS:
        raise HTTPException(422, f"Normalizador inválido: {', '.join(NORMALIZERS)}")
    if body.key != t.key:
        raise HTTPException(422, "La clave no se puede cambiar (las llaves guardadas dependen de ella)")
    for k, v in body.model_dump(exclude={"archived", "key"}).items():
        setattr(t, k, v)
    t.archived_at = utcnow() if body.archived else None
    await session.commit()
    return _kt_out(t)


# --- Ficha maestra de un cliente -------------------------------------------------------------------
def _key_out(k: ContactKey, t: GoldenKeyType | None, sees: bool) -> dict:
    sensitive = bool(t and t.is_sensitive) and not sees
    normalizer = t.normalizer if t else "text"
    return {"id": k.id, "key_type": k.key_type, "label": t.label if t else k.key_type, "subtype": k.subtype,
            "value": mask(normalizer, k.value) if sensitive else k.value,
            "value_normalized": mask(normalizer, k.value_normalized) if sensitive else k.value_normalized,
            "rank": k.rank, "status": k.status, "verified": k.verified, "confidence": k.confidence,
            "source": k.source, "evidence": None if sensitive else k.evidence, "conversation_id": k.conversation_id,
            "message_id": k.message_id, "first_seen_at": k.first_seen_at, "last_seen_at": k.last_seen_at,
            "seen_count": k.seen_count, "masked": sensitive}


def _vehicle_out(v: ContactVehicle) -> dict:
    return {"id": v.id, "contact_id": v.contact_id, "plate": v.plate, "vin": v.vin,
            **{f: getattr(v, f) for f in VEHICLE_FIELDS}, "attributes": v.attributes or {}, "source": v.source,
            "confidence": v.confidence, "conversation_id": v.conversation_id, "created_at": v.created_at,
            "updated_at": v.updated_at}


def _consent_out(c: ContactConsent) -> dict:
    return {"id": c.id, "consent_type": c.consent_type, "granted": c.granted, "policy_version": c.policy_version,
            "source": c.source, "recorded_at": c.recorded_at, "revoked_at": c.revoked_at, "evidence": c.evidence,
            "conversation_id": c.conversation_id}


def _golden_out(g: ContactGolden | None, sees: bool) -> dict:
    if not g:
        return {}
    out = {c.name: getattr(g, c.name) for c in ContactGolden.__table__.columns}
    if not sees:
        out["document_number"] = mask("document", g.document_number)
        out["birthdate"] = mask("date", g.birthdate.isoformat()) if g.birthdate else None
        out["addresses"] = [{**a, "value": mask("address", a.get("value")), "line": None}
                            for a in (g.addresses or []) if isinstance(a, dict)]
        out["masked"] = True
    return out


@router.get("/contacts/{contact_id}/golden")
async def contact_golden(contact_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    sees = await _sees(session, agent)
    types = await key_types(session, contact.organization_id, include_archived=True)
    keys = (await session.scalars(select(ContactKey).where(ContactKey.contact_id == contact.id)
                                  .order_by(ContactKey.key_type, ContactKey.rank, ContactKey.last_seen_at.desc()))).all()
    vehicles = (await session.scalars(select(ContactVehicle).where(ContactVehicle.contact_id == contact.id)
                                      .order_by(ContactVehicle.updated_at.desc()))).all()
    consents = (await session.scalars(select(ContactConsent).where(ContactConsent.contact_id == contact.id)
                                      .order_by(ContactConsent.recorded_at.desc()))).all()
    extractions = (await session.scalars(select(KeyExtraction).where(KeyExtraction.contact_id == contact.id)
                                         .order_by(KeyExtraction.created_at.desc()).limit(50))).all()
    golden = await session.get(ContactGolden, contact.id)
    return {"golden": _golden_out(golden, sees),
            "keys": [_key_out(k, types.get(k.key_type), sees) for k in keys],
            "vehicles": [_vehicle_out(v) for v in vehicles],
            "consents": [_consent_out(c) for c in consents],
            "extractions": [{"id": e.id, "source_kind": e.source_kind, "document_type": e.document_type,
                             "status": e.status, "keys_added": e.keys_added, "keys_updated": e.keys_updated,
                             "conversation_id": e.conversation_id, "created_at": e.created_at} for e in extractions],
            "completeness_pct": golden.completeness_pct if golden else 0, "can_see_sensitive": sees}


class KeyIn(BaseModel):
    key_type: str
    subtype: str | None = None
    value: str = Field(min_length=1, max_length=500)
    rank: str | None = None


@router.post("/contacts/{contact_id}/keys")
async def add_key(contact_id: int, body: KeyIn, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    if body.rank not in (None, "primary", "secondary"):
        raise HTTPException(422, "Rango inválido")
    try:
        res = await upsert_key(session, contact, body.key_type, body.value, subtype=body.subtype, source="agent",
                               verified=True, rank=body.rank, agent_id=agent.id)
    except GoldenError as e:
        raise HTTPException(422, str(e)) from None
    if res.key is None:
        raise HTTPException(422, "El valor no tiene un formato válido para esa llave")
    await session.commit()
    types = await key_types(session, contact.organization_id)
    return _key_out(res.key, types.get(res.key.key_type), await _sees(session, agent))


async def _key(session: AsyncSession, key_id: int, agent: Agent) -> ContactKey:
    k = await session.get(ContactKey, key_id)
    if not k or k.organization_id != agent.organization_id:
        raise HTTPException(404, "Llave no encontrada")
    return k


class KeyPatch(BaseModel):
    rank: str | None = None
    status: str | None = None
    verified: bool | None = None
    value: str | None = None


@router.patch("/contact-keys/{key_id}")
async def patch_key(key_id: int, body: KeyPatch, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    k = await _key(session, key_id, agent)
    contact = await session.get(Contact, k.contact_id)
    if body.value is not None and body.value.strip() and body.value.strip() != k.value:
        # Corregir un valor = el nuevo entra como del asesor (verificado) y el anterior se rechaza
        try:
            res = await upsert_key(session, contact, k.key_type, body.value, subtype=k.subtype, source="agent",
                                   verified=True, rank="primary" if k.rank == "primary" else None, agent_id=agent.id)
        except GoldenError as e:
            raise HTTPException(422, str(e)) from None
        if res.key is None:
            raise HTTPException(422, "El valor no tiene un formato válido para esa llave")
        if res.key.id != k.id:
            k.status, k.rank = "rejected", "secondary"
            await session.flush()
            k = res.key
    if body.status is not None:
        if body.status not in ("active", "superseded", "rejected"):
            raise HTTPException(422, "Estado inválido")
        k.status = body.status
        if body.status != "active":
            k.rank = "secondary"
    if body.verified is not None:
        k.verified = body.verified
    if body.rank is not None:
        if body.rank not in ("primary", "secondary"):
            raise HTTPException(422, "Rango inválido")
        if body.rank == "primary" and k.status == "active":
            await upsert_key(session, contact, k.key_type, k.value, subtype=k.subtype, source="agent", verified=True,
                             rank="primary", agent_id=agent.id)
        elif body.rank == "secondary":
            k.rank = "secondary"
    await session.commit()
    await session.refresh(k)
    types = await key_types(session, k.organization_id, include_archived=True)
    return _key_out(k, types.get(k.key_type), await _sees(session, agent))


@router.delete("/contact-keys/{key_id}")
async def delete_key(key_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Borrado lógico: la llave queda rechazada (la IA no la vuelve a activar)."""
    k = await _key(session, key_id, agent)
    k.status, k.rank = "rejected", "secondary"
    await session.commit()
    return {"ok": True}


# --- Vehículos ---------------------------------------------------------------------------------------
class VehicleIn(BaseModel):
    plate: str | None = None
    vin: str | None = None
    make: str | None = None
    model: str | None = None
    version: str | None = None
    year: int | None = None
    color: str | None = None
    fuel: str | None = None
    mileage_km: int | None = None
    relation: str | None = None
    status: str | None = None
    insurance_due: date | None = None
    inspection_due: date | None = None
    warranty_until: date | None = None
    next_service_at: date | None = None
    attributes: dict | None = None


@router.get("/contacts/{contact_id}/vehicles")
async def list_vehicles(contact_id: int, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    rows = (await session.scalars(select(ContactVehicle).where(ContactVehicle.contact_id == contact.id)
                                  .order_by(ContactVehicle.updated_at.desc()))).all()
    return [_vehicle_out(v) for v in rows]


@router.post("/contacts/{contact_id}/vehicles")
async def create_vehicle(contact_id: int, body: VehicleIn, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    attrs = body.model_dump(exclude={"plate", "vin", "attributes"}, exclude_none=True)
    try:
        v = await upsert_vehicle(session, contact, plate=body.plate, vin=body.vin, attrs=attrs, source="agent",
                                 extra=body.attributes, overwrite=True)
    except RecordError as e:
        raise HTTPException(422, str(e)) from None
    if not v:
        raise HTTPException(422, "Indica una placa o un VIN válido")
    await session.commit()
    await session.refresh(v)
    return _vehicle_out(v)


async def _vehicle(session: AsyncSession, vehicle_id: int, agent: Agent) -> ContactVehicle:
    v = await session.get(ContactVehicle, vehicle_id)
    if not v or v.organization_id != agent.organization_id:
        raise HTTPException(404, "Vehículo no encontrado")
    return v


@router.patch("/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: int, body: VehicleIn, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    v = await _vehicle(session, vehicle_id, agent)
    contact = await session.get(Contact, v.contact_id)
    data = body.model_dump(exclude_unset=True)
    for ident in ("plate", "vin"):
        if data.get(ident):
            norm = (nz.plate if ident == "plate" else nz.vin)(data[ident])
            if not norm:
                raise HTTPException(422, f"{'Placa' if ident == 'plate' else 'VIN'} inválido")
            setattr(v, ident, norm.value)
            await upsert_key(session, contact, ident, norm.value, source="agent", verified=True, agent_id=agent.id)
    from app.golden.records import _clean_vehicle

    for k, val in _clean_vehicle({k: data[k] for k in VEHICLE_FIELDS if k in data}).items():
        setattr(v, k, val)
    if "attributes" in data and isinstance(data["attributes"], dict):
        v.attributes = {**(v.attributes or {}), **data["attributes"]}
    v.source, v.updated_at = "agent", utcnow()
    await session.commit()
    await session.refresh(v)
    return _vehicle_out(v)


@router.delete("/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    v = await _vehicle(session, vehicle_id, agent)
    await session.delete(v)
    await session.commit()
    return {"ok": True}


# --- Consentimientos -----------------------------------------------------------------------------------
class ConsentIn(BaseModel):
    consent_type: str
    granted: bool
    policy_version: str | None = None
    evidence: str | None = None
    conversation_id: int | None = None


@router.get("/contacts/{contact_id}/consents")
async def list_consents(contact_id: int, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    rows = (await session.scalars(select(ContactConsent).where(ContactConsent.contact_id == contact.id)
                                  .order_by(ContactConsent.recorded_at.desc()))).all()
    return [_consent_out(c) for c in rows]


@router.post("/contacts/{contact_id}/consents")
async def add_consent(contact_id: int, body: ConsentIn, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    try:
        c = await record_consent(session, contact, body.consent_type, body.granted, source="agent",
                                 policy_version=body.policy_version, evidence=body.evidence,
                                 conversation_id=body.conversation_id, agent_id=agent.id)
    except RecordError as e:
        raise HTTPException(422, str(e)) from None
    await session.commit()
    return _consent_out(c)


@router.post("/consents/{consent_id}/revoke")
async def revoke_consent(consent_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    c = await session.get(ContactConsent, consent_id)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Consentimiento no encontrado")
    c.revoked_at = c.revoked_at or utcnow()
    await session.commit()
    return _consent_out(c)


# --- Búsqueda por llave ---------------------------------------------------------------------------------
@router.get("/golden/search")
async def search(q: str, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Encuentra clientes por placa, VIN, documento, correo, teléfono o usuario."""
    org = agent.organization_id
    q = (q or "").strip()
    if len(q) < 3:
        return []
    sees = await _sees(session, agent)
    candidates: list[tuple[str, str]] = []
    for kind, fn in (("plate", nz.plate), ("vin", nz.vin), ("email", nz.email)):
        n = fn(q)
        if n:
            candidates.append((kind, n.value))
    n = nz.phone(q)
    if n:
        candidates.append(("phone", n.value))
    digits = re.sub(r"\D", "", q)
    if 5 <= len(digits) <= 15:
        candidates.append(("document", digits))
    u = nz.username(q)
    if u:
        candidates.append(("username", u.value))
    if not candidates:
        return []
    rows = (await session.execute(
        select(ContactKey.contact_id, ContactKey.key_type, ContactKey.value, Contact.name)
        .join(Contact, Contact.id == ContactKey.contact_id)
        .where(ContactKey.organization_id == org, ContactKey.status == "active",
               or_(*[(ContactKey.key_type == k) & (ContactKey.value_normalized == v) for k, v in candidates]))
        .order_by(ContactKey.rank, ContactKey.last_seen_at.desc()).limit(50))).all()
    out, seen = [], set()
    for cid, key_type, value, name in rows:
        if (cid, key_type) in seen:
            continue
        seen.add((cid, key_type))
        sensitive = key_type in ("document", "birthdate", "address") and not sees
        out.append({"contact_id": cid, "name": name, "matched_on": key_type,
                    "value": mask("document" if key_type == "document" else "text", value) if sensitive else value})
    return out


# --- Duplicados -----------------------------------------------------------------------------------------
async def _summary(session: AsyncSession, contact_id: int, sees: bool) -> dict:
    c = await session.get(Contact, contact_id)
    g = await session.get(ContactGolden, contact_id)
    return {"id": contact_id, "name": c.name if c else None, "phone": c.wa_id if c else None,
            "email": c.email if c else None, "created_at": c.created_at if c else None,
            "last_interaction_at": c.last_interaction_at if c else None, "golden": _golden_out(g, sees)}


@router.get("/golden/merge-candidates")
async def merge_candidates(status: str = "pending", agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    if agent.role not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo administradores y supervisores")
    rows = (await session.scalars(select(ContactMergeCandidate).where(
        ContactMergeCandidate.organization_id == agent.organization_id, ContactMergeCandidate.status == status)
        .order_by(ContactMergeCandidate.score.desc(), ContactMergeCandidate.id).limit(200))).all()
    sees = await _sees(session, agent)
    out = []
    for r in rows:
        matched = [{**m, "value_normalized": mask(m["key_type"] == "document" and "document" or "text",
                                                  m["value_normalized"]) if (m["key_type"] == "document" and not sees)
                    else m["value_normalized"]} for m in r.matched]
        out.append({"id": r.id, "score": r.score, "status": r.status, "matched": matched, "created_at": r.created_at,
                    "contact_a": await _summary(session, r.contact_a_id, sees),
                    "contact_b": await _summary(session, r.contact_b_id, sees)})
    return out


class MergeIn(BaseModel):
    keep: int


@router.post("/golden/merge-candidates/{cand_id}/merge")
async def merge(cand_id: int, body: MergeIn, agent: Agent = Depends(current_agent),
                session: AsyncSession = Depends(get_session)):
    if agent.role not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo administradores y supervisores")
    cand = await session.get(ContactMergeCandidate, cand_id)
    if not cand or cand.organization_id != agent.organization_id or cand.status != "pending":
        raise HTTPException(404, "Candidato no encontrado")
    if body.keep not in (cand.contact_a_id, cand.contact_b_id):
        raise HTTPException(422, "keep debe ser uno de los dos clientes")
    drop_id = cand.contact_b_id if body.keep == cand.contact_a_id else cand.contact_a_id
    keep, drop = await session.get(Contact, body.keep), await session.get(Contact, drop_id)
    cand.status, cand.decided_by, cand.decided_at = "merged", agent.id, utcnow()
    # Los principales del que se va pasan a secundarios donde el que queda ya tiene principal (no se pierden)
    await session.execute(text("""
        update public.contact_keys d set rank = 'secondary'
        where d.contact_id = :drop and d.rank = 'primary' and exists (
          select 1 from public.contact_keys k where k.contact_id = :keep and k.key_type = d.key_type
            and coalesce(k.subtype, '') = coalesce(d.subtype, '') and k.rank = 'primary' and k.status = 'active')"""),
        {"drop": drop_id, "keep": body.keep})
    await session.flush()
    from app.identity import merge_contacts

    keep = await merge_contacts(session, keep, drop)  # mueve conversaciones, llaves, vehículos, consentimientos…
    await refresh_golden(session, keep.id)
    await session.commit()
    return {"ok": True, "contact_id": keep.id, "merged_contact_id": drop_id}


@router.post("/golden/merge-candidates/{cand_id}/dismiss")
async def dismiss(cand_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if agent.role not in ("admin", "supervisor"):
        raise HTTPException(403, "Solo administradores y supervisores")
    cand = await session.get(ContactMergeCandidate, cand_id)
    if not cand or cand.organization_id != agent.organization_id:
        raise HTTPException(404, "Candidato no encontrado")
    cand.status, cand.decided_by, cand.decided_at = "dismissed", agent.id, utcnow()
    await session.commit()
    return {"ok": True}


# --- Organización de campos -------------------------------------------------------------------------------
@router.get("/golden/fields")
async def fields_by_section(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Campos activos agrupados por sección, con alcance, destino y alias."""
    rows = (await session.scalars(select(ContactField).where(
        ContactField.organization_id == agent.organization_id, ContactField.archived_at.is_(None))
        .order_by(ContactField.position, ContactField.id))).all()
    sections: dict[str, list[dict]] = {}
    for f in rows:
        sections.setdefault(f.section or "General", []).append(
            {"id": f.id, "key": f.key, "label": f.label, "type": f.type, "scope": f.scope, "pipeline": f.pipeline,
             "maps_to": f.maps_to, "aliases": list(f.aliases or []), "show_in_card": f.show_in_card,
             "options": f.options, "currency": f.currency, "ai_extract": f.ai_extract})
    order = {s: i for i, s in enumerate(SECTIONS)}
    return {"sections": [{"section": s, "fields": fs} for s, fs in sorted(sections.items(),
                                                                          key=lambda kv: order.get(kv[0], 99))],
            "scopes": list(SCOPES), "pipelines": list(PIPELINES), "section_names": list(SECTIONS)}


@router.get("/golden/presets")
async def presets(_: Agent = Depends(current_agent)):
    return list_presets()


@router.get("/golden/presets/{key}")
async def preset(key: str, _: Agent = Depends(current_agent)):
    try:
        data = load_preset(key)
    except KeyError:
        raise HTTPException(404, "Plantilla no encontrada") from None
    clean, errors = validate_proposal(data.get("fields", []))
    return {"key": key, "name": data.get("name"), "description": data.get("description"), "fields": clean,
            "errors": errors}


class ConsolidateIn(BaseModel):
    fields: list[str] | None = None
    use_existing: bool = False


@router.post("/golden/fields/consolidate")
async def consolidate_fields(body: ConsolidateIn, agent: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    """Propuesta (con IA) de campos canónicos: agrupa duplicados, define sección, alcance, destino y alias."""
    names = list(body.fields or [])
    if body.use_existing:
        rows = (await session.scalars(select(ContactField).where(
            ContactField.organization_id == agent.organization_id, ContactField.archived_at.is_(None)))).all()
        names += [f"{f.label} ({f.key})" if f.label != f.key else f.key for f in rows]
    if not names:
        raise HTTPException(422, "Indica los campos o usa los existentes")
    try:
        return await consolidate(session, agent.organization_id, names)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


class ApplyIn(BaseModel):
    """Acepta {fields: [...]} o {proposal: {fields: [...]}} (lo que devuelve consolidate o un preset)."""

    fields: list[dict] | None = None
    proposal: dict | None = None

    def items(self) -> list[dict]:
        if self.fields is not None:
            return self.fields
        return list((self.proposal or {}).get("fields") or [])


@router.post("/golden/fields/apply")
async def apply_fields(body: ApplyIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """Aplica una propuesta o plantilla: crea/actualiza campos, migra valores de los alias y archiva duplicados."""
    items = body.items()
    if not items:
        raise HTTPException(422, "La propuesta no trae campos")
    stats = await apply_proposal(session, agent.organization_id, items, agent.id)
    await session.commit()
    return stats


# --- Reporte ----------------------------------------------------------------------------------------------
@router.get("/reports/golden")
async def golden_report(days: int = 30, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    cov = (await session.execute(text("select * from reporting.v_golden_coverage where organization_id = :o"),
                                 {"o": org})).mappings().first()
    per_day = (await session.execute(text("""
        select (e.created_at at time zone o.timezone)::date as day, count(*) as extractions,
               count(*) filter (where e.status = 'done') as done, count(*) filter (where e.status = 'failed') as failed,
               count(*) filter (where e.source_kind in ('document', 'image')) as documents,
               count(*) filter (where e.source_kind = 'conversation') as conversations,
               coalesce(sum(e.keys_added), 0) as keys_added
        from public.key_extractions e join public.organizations o on o.id = e.organization_id
        where e.organization_id = :o and e.created_at >= :s group by 1 order by 1"""), {"o": org, "s": since})).mappings()
    doc_types = (await session.execute(
        select(KeyExtraction.document_type, func.count()).where(
            KeyExtraction.organization_id == org, KeyExtraction.document_type.is_not(None),
            KeyExtraction.created_at >= since).group_by(KeyExtraction.document_type)
        .order_by(func.count().desc()))).all()
    by_source = (await session.execute(
        select(ContactKey.source, func.count()).where(ContactKey.organization_id == org, ContactKey.status == "active")
        .group_by(ContactKey.source))).all()
    by_type = (await session.execute(
        select(ContactKey.key_type, func.count(func.distinct(ContactKey.contact_id))).where(
            ContactKey.organization_id == org, ContactKey.status == "active").group_by(ContactKey.key_type))).all()
    pending = await session.scalar(select(func.count()).select_from(ContactMergeCandidate).where(
        ContactMergeCandidate.organization_id == org, ContactMergeCandidate.status == "pending")) or 0
    vehicles = await session.scalar(select(func.count()).select_from(ContactVehicle).where(
        ContactVehicle.organization_id == org, ContactVehicle.status == "active")) or 0
    return {"coverage": dict(cov) if cov else {"contacts": 0, "avg_completeness": 0},
            "extractions_series": (series := [dict(r) for r in per_day]),
            "extractions_per_day": series,
            "document_types": [{"document_type": d, "count": n} for d, n in doc_types],
            "keys_by_source": [{"source": s, "count": n} for s, n in by_source],
            "contacts_by_key_type": [{"key_type": k, "contacts": n} for k, n in by_type],
            "merge_pending": pending, "merge_candidates_pending": pending, "vehicles": vehicles}
