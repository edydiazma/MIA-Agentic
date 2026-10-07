"""Llaves del registro maestro: alta idempotente, rango principal/secundario, cruce de duplicados y enmascarado.

Reglas de rango (docs/data-model.md §15):
- El primer valor de un tipo es el principal. Un valor verificado (canal, asesor) o de una fuente más confiable con
  bastante más confianza reemplaza a un principal no verificado. Un principal verificado nunca se baja solo.
- Tipos de un solo valor (nombres, apellidos, fecha de nacimiento…): si llega otro valor distinto, ambos quedan;
  el anterior pasa a "superseded" solo cuando gana una persona o una fuente verificada.
- Cada cambio de principal queda en contact_changes ("golden.<tipo>").
- Al agregar una llave identificadora (documento, correo, teléfono, usuario, placa, VIN) se buscan otros clientes con
  la misma llave → contact_merge_candidates.
"""

from dataclasses import dataclass

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.golden import normalize as nz
from app.models import (
    Agent,
    Contact,
    ContactChange,
    ContactKey,
    ContactMergeCandidate,
    GoldenKeyType,
    Organization,
    utcnow,
)

# Peso de cada llave para decidir si dos clientes son la misma persona
MATCH_SCORES = {"document": 0.95, "email": 0.85, "phone": 0.8, "username": 0.7, "vin": 0.5, "plate": 0.4}
# Confianza de la fuente (desempata el principal)
SOURCE_TRUST = {"agent": 5, "whatsapp": 5, "channel": 5, "ai_document": 4, "crm": 3, "import": 3, "api": 3,
                "form": 3, "flow": 3, "ai_conversation": 2}
CHANGE_SOURCE = {"ai_conversation": "ai", "ai_document": "ai", "agent": "agent", "whatsapp": "whatsapp",
                 "channel": "whatsapp", "import": "import", "api": "api", "crm": "crm", "flow": "flow", "form": "flow"}
# El principal se elige por subtipo (una dirección principal de casa y otra de trabajo; un usuario por red)
PRIMARY_BY_SUBTYPE = {"address", "username"}
SOURCES = tuple(SOURCE_TRUST)


class GoldenError(Exception):
    pass


@dataclass
class UpsertResult:
    key: ContactKey | None
    created: bool = False
    promoted: bool = False


async def key_types(session: AsyncSession, org: int, include_archived: bool = False) -> dict[str, GoldenKeyType]:
    """Tipos del sistema + los de la empresa (una fila de la empresa con la misma clave reemplaza a la del sistema)."""
    rows = (await session.scalars(
        select(GoldenKeyType).where(or_(GoldenKeyType.organization_id.is_(None), GoldenKeyType.organization_id == org))
        .order_by(GoldenKeyType.organization_id.nulls_first(), GoldenKeyType.position, GoldenKeyType.id))).all()
    out: dict[str, GoldenKeyType] = {}
    for r in rows:
        out[r.key] = r  # la de la empresa llega después y gana
    if not include_archived:
        out = {k: v for k, v in out.items() if v.archived_at is None}
    return dict(sorted(out.items(), key=lambda kv: (kv[1].position, kv[0])))


async def org_country(session: AsyncSession, org: int) -> str:
    return (await session.scalar(select(Organization.country).where(Organization.id == org)) or "CO").upper()


def _primary_scope(kt: GoldenKeyType, subtype: str | None):
    cond = [ContactKey.key_type == kt.key, ContactKey.rank == "primary", ContactKey.status == "active"]
    if kt.key in PRIMARY_BY_SUBTYPE:
        cond.append(ContactKey.subtype.is_(None) if subtype is None else ContactKey.subtype == subtype)
    return cond


def _wins(new_source: str, new_conf: float | None, new_verified: bool, cur: ContactKey) -> bool:
    """¿El valor nuevo debe reemplazar al principal actual?"""
    if cur.verified and not new_verified:
        return False
    if new_verified and not cur.verified:
        return True
    nt, ct = SOURCE_TRUST.get(new_source, 1), SOURCE_TRUST.get(cur.source, 1)
    if nt != ct:
        return nt > ct and (new_conf or 0.7) >= (cur.confidence or 0.7) - 0.1
    return (new_conf or 0) > (cur.confidence or 0) + 0.15


async def upsert_key(
    session: AsyncSession, contact: Contact, key_type: str, value, *, source: str, subtype: str | None = None,
    confidence: float | None = None, verified: bool = False, rank: str | None = None, data: dict | None = None,
    conversation_id: int | None = None, message_id: int | None = None, extraction_id: int | None = None,
    evidence: str | None = None, agent_id: int | None = None, country: str | None = None,
    types: dict[str, GoldenKeyType] | None = None,
) -> UpsertResult:
    """Normaliza y guarda una llave del cliente. Devuelve la fila (None si el valor no sirve)."""
    if source not in SOURCE_TRUST:
        raise GoldenError(f"Fuente inválida: {source}")
    types = types or await key_types(session, contact.organization_id)
    kt = types.get(key_type)
    if not kt:
        raise GoldenError(f"Tipo de llave desconocido: {key_type}")
    norm = nz.normalize(kt.normalizer, value, subtype, country or await org_country(session, contact.organization_id))
    if not norm or not norm.value:
        return UpsertResult(None)
    sub = norm.subtype if norm.subtype is not None else subtype
    sub = (sub or None) and str(sub)[:40]
    conf = None if confidence is None else max(0.0, min(1.0, float(confidence) * norm.factor))
    display = str(value).strip()[:500]
    if kt.normalizer == "name":
        display = norm.data.get("display") or display
    elif kt.normalizer == "address":
        display = norm.data.get("line") or display
    elif kt.normalizer in ("plate", "vin", "email", "date"):
        display = norm.value
    merged_data = {**{k: v for k, v in norm.data.items() if k not in ("display",)}, **(data or {})}
    now = utcnow()

    row = await session.scalar(select(ContactKey).where(
        ContactKey.contact_id == contact.id, ContactKey.key_type == kt.key,
        ContactKey.subtype.is_(None) if sub is None else ContactKey.subtype == sub,
        ContactKey.value_normalized == norm.value))
    created = False
    if row:
        row.seen_count += 1
        row.last_seen_at = now
        row.verified = row.verified or verified
        if conf is not None and (row.confidence is None or conf > row.confidence):
            row.confidence = conf
        if row.status == "rejected" and source == "agent":
            row.status = "active"  # la persona lo vuelve a confirmar
        row.evidence = row.evidence or (evidence or None)
        row.data = {**(row.data or {}), **merged_data}
    else:
        row = ContactKey(organization_id=contact.organization_id, contact_id=contact.id, key_type=kt.key, subtype=sub,
                         value=display, value_normalized=norm.value, data=merged_data, rank="secondary",
                         status="active", verified=verified, confidence=conf, source=source,
                         conversation_id=conversation_id, message_id=message_id, extraction_id=extraction_id,
                         evidence=(evidence or None) and evidence[:500], created_by=agent_id,
                         first_seen_at=now, last_seen_at=now)
        session.add(row)
        created = True
    await session.flush()

    promoted = False
    if row.status == "active":
        current = (await session.scalars(select(ContactKey).where(
            ContactKey.contact_id == contact.id, ContactKey.id != row.id, *_primary_scope(kt, sub)))).all()
        human = source == "agent" or verified
        if row.rank != "primary" and (rank == "primary" or not current
                                      or all(_wins(source, conf, verified, c) for c in current)):
            for c in current:
                c.rank = "secondary"
                if not kt.multi and human:
                    c.status = "superseded"
            await session.flush()
            row.rank = "primary"
            promoted = True
            old = current[0].value if current else None
            session.add(ContactChange(organization_id=contact.organization_id, contact_id=contact.id,
                                      field_key=f"golden.{kt.key}", old_value=old, new_value=row.value,
                                      source=CHANGE_SOURCE.get(source, "system"), agent_id=agent_id,
                                      conversation_id=conversation_id))
        elif rank == "secondary" and row.rank == "primary" and source == "agent":
            row.rank = "secondary"
    await session.flush()
    await _mirror(session, contact, kt.key, row)
    if kt.is_identifier and row.status == "active":
        await detect_duplicates(session, contact.id, contact.organization_id, kt.key, norm.value)
    return UpsertResult(row, created, promoted)


async def _mirror(session: AsyncSession, contact: Contact, key_type: str, row: ContactKey) -> None:
    """Completa la ficha básica con el registro maestro (solo campos vacíos)."""
    if key_type == "email" and not contact.email and row.status == "active":
        contact.email = row.value_normalized
    elif key_type in ("first_name", "last_name") and not contact.name and row.rank == "primary":
        names = (await session.execute(select(ContactKey.key_type, ContactKey.value).where(
            ContactKey.contact_id == contact.id, ContactKey.key_type.in_(("first_name", "last_name")),
            ContactKey.rank == "primary", ContactKey.status == "active"))).all()
        parts = dict(names)
        if parts.get("first_name") and parts.get("last_name"):  # solo con nombre y apellido (no un apodo suelto)
            contact.name = f"{parts['first_name']} {parts['last_name']}"


async def detect_duplicates(session: AsyncSession, contact_id: int, org: int, key_type: str, value: str) -> int:
    """Otros clientes con la misma llave identificadora → candidato a fusión (puntaje combinado)."""
    others = (await session.scalars(select(ContactKey.contact_id).where(
        ContactKey.organization_id == org, ContactKey.key_type == key_type, ContactKey.value_normalized == value,
        ContactKey.status == "active", ContactKey.contact_id != contact_id).distinct())).all()
    for other in others:
        a, b = sorted((contact_id, other))
        cand = await session.scalar(select(ContactMergeCandidate).where(
            ContactMergeCandidate.contact_a_id == a, ContactMergeCandidate.contact_b_id == b))
        if cand and cand.status != "pending":
            continue
        matched = list(cand.matched) if cand else []
        if not any(m.get("key_type") == key_type and m.get("value_normalized") == value for m in matched):
            matched.append({"key_type": key_type, "value_normalized": value})
        miss = 1.0
        for m in matched:
            miss *= 1 - MATCH_SCORES.get(m["key_type"], 0.3)
        score = round(1 - miss, 3)
        if cand:
            cand.matched, cand.score = matched, score
        else:
            session.add(ContactMergeCandidate(organization_id=org, contact_a_id=a, contact_b_id=b, matched=matched,
                                              score=score))
    await session.flush()
    return len(others)


async def refresh_golden(session: AsyncSession, contact_id: int) -> None:
    await session.execute(text("select private.refresh_contact_golden(:c)"), {"c": contact_id})


# --- Enmascarado ----------------------------------------------------------------------------------
def can_see_sensitive(agent: Agent, settings: dict) -> bool:
    return agent.role in ("admin", "supervisor") or bool(settings.get("agents_see_sensitive"))


def mask(normalizer: str, value: str | None) -> str | None:
    if not value:
        return value
    if normalizer == "date":
        return "****" + value[4:] if len(value) >= 10 else "****"
    if normalizer == "address":
        return value.split(",")[-1].strip() if "," in value else "******"
    keep = 4 if len(value) > 6 else 2
    return "*" * max(4, len(value) - keep) + value[-keep:]
