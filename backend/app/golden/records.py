"""Vehículos y consentimientos del cliente (registro maestro, docs/data-model.md §15)."""

import re
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.golden import normalize as nz
from app.golden.keys import upsert_key
from app.models import Contact, ContactConsent, ContactVehicle, utcnow

VEHICLE_FIELDS = ("make", "model", "version", "year", "color", "fuel", "mileage_km", "relation", "status",
                  "insurance_due", "inspection_due", "warranty_until", "next_service_at")
VEHICLE_SOURCES = ("ai_conversation", "ai_document", "agent", "import", "api", "crm", "flow")
DATE_FIELDS = ("insurance_due", "inspection_due", "warranty_until", "next_service_at")
CONSENT_TYPES = ("habeas_data", "terms", "marketing", "data_sharing", "call_recording")
CONSENT_SOURCES = ("whatsapp", "flow", "agent", "form", "import", "api", "ai")
YES = {"si", "sí", "acepto", "de acuerdo", "ok", "okay", "claro", "listo", "dale", "yes", "autorizo", "confirmo",
       "si acepto", "sí acepto", "1", "true", "afirmativo", "por supuesto", "correcto"}
NO = {"no", "no acepto", "no autorizo", "rechazo", "nop", "negativo", "0", "false", "no gracias"}


class RecordError(Exception):
    pass


def parse_yes_no(value) -> bool | None:
    if isinstance(value, bool):
        return value
    v = nz.fold(str(value or "")).replace("#", "").strip()
    if not v:
        return None
    if v in {nz.fold(x) for x in YES} or v.startswith(("si ", "acepto", "autorizo")):
        return True
    if v in {nz.fold(x) for x in NO} or v.startswith(("no ", "no,")):
        return False
    return None


def _clean_vehicle(attrs: dict) -> dict:
    out: dict = {}
    for k in VEHICLE_FIELDS:
        v = attrs.get(k)
        if v in (None, ""):
            continue
        if k == "year":
            m = re.search(r"(19[5-9]\d|20\d\d|2100)", str(v))
            if m:
                out[k] = int(m.group(1))
        elif k == "mileage_km":
            digits = re.sub(r"\D", "", str(v))
            if digits:
                out[k] = int(digits[:9])
        elif k in DATE_FIELDS:
            d = v if isinstance(v, date) else nz.parse_date(str(v))
            if d:
                out[k] = d
        elif k == "relation":
            if v in ("owner", "driver", "interested", "previous_owner"):
                out[k] = v
        elif k == "status":
            if v in ("active", "sold", "inactive"):
                out[k] = v
        else:
            out[k] = nz.title_name(str(v))[:80] if k in ("make", "model") else str(v).strip()[:80]
    return out


async def upsert_vehicle(session: AsyncSession, contact: Contact, *, plate: str | None = None, vin: str | None = None,
                         attrs: dict | None = None, source: str, confidence: float | None = None,
                         conversation_id: int | None = None, extraction_id: int | None = None,
                         extra: dict | None = None, overwrite: bool = False) -> ContactVehicle | None:
    """Crea o completa el vehículo del cliente (por placa o VIN). También registra la placa y el VIN como llaves."""
    if source not in VEHICLE_SOURCES:
        raise RecordError(f"Fuente inválida: {source}")
    p = nz.plate(plate) if plate else None
    v = nz.vin(vin) if vin else None
    if not p and not v:
        return None
    conds = []
    if p:
        conds.append(ContactVehicle.plate == p.value)
    if v:
        conds.append(ContactVehicle.vin == v.value)
    row = (await session.scalars(select(ContactVehicle).where(ContactVehicle.contact_id == contact.id, or_(*conds))
                                 .order_by(ContactVehicle.id))).first()
    clean = _clean_vehicle(attrs or {})
    if not row:
        row = ContactVehicle(organization_id=contact.organization_id, contact_id=contact.id,
                             plate=p.value if p else None, vin=v.value if v else None, source=source,
                             confidence=confidence, conversation_id=conversation_id, extraction_id=extraction_id,
                             attributes=extra or {}, **clean)
        session.add(row)
    else:
        if p and not row.plate:
            row.plate = p.value
        if v and not row.vin:
            row.vin = v.value
        for k, val in clean.items():
            cur = getattr(row, k)
            if k in DATE_FIELDS and cur and val and not overwrite:
                setattr(row, k, max(cur, val))  # el vencimiento más reciente (renovación)
            elif cur in (None, "") or overwrite:
                setattr(row, k, val)
        if extra:
            row.attributes = {**(row.attributes or {}), **extra}
        if confidence is not None and (row.confidence or 0) < confidence:
            row.confidence = confidence
        row.updated_at = utcnow()
    await session.flush()
    key_source = {"ai_conversation": "ai_conversation", "ai_document": "ai_document"}.get(source, source)
    for kt, val in (("plate", p), ("vin", v)):
        if val:
            await upsert_key(session, contact, kt, val.value, source=key_source, confidence=confidence,
                             verified=source == "agent", conversation_id=conversation_id, extraction_id=extraction_id)
    return row


async def record_consent(session: AsyncSession, contact: Contact, consent_type: str, granted: bool, *, source: str,
                         policy_version: str | None = None, channel_id: int | None = None,
                         conversation_id: int | None = None, message_id: int | None = None,
                         evidence: str | None = None, agent_id: int | None = None) -> ContactConsent:
    consent_type = (consent_type or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", consent_type):
        raise RecordError("Tipo de consentimiento inválido")
    if source not in CONSENT_SOURCES:
        raise RecordError(f"Fuente inválida: {source}")
    row = ContactConsent(organization_id=contact.organization_id, contact_id=contact.id, consent_type=consent_type,
                         granted=bool(granted), policy_version=policy_version, channel_id=channel_id,
                         conversation_id=conversation_id, message_id=message_id,
                         evidence=(evidence or None) and str(evidence)[:1000], source=source, recorded_by=agent_id)
    session.add(row)
    await session.flush()
    return row


async def current_consents(session: AsyncSession, contact_id: int) -> dict[str, ContactConsent]:
    rows = (await session.scalars(select(ContactConsent).where(
        ContactConsent.contact_id == contact_id, ContactConsent.revoked_at.is_(None))
        .order_by(ContactConsent.recorded_at))).all()
    return {r.consent_type: r for r in rows}  # el último gana
