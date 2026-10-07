"""Ficha del cliente: campos personalizados tipados (contact_field_values) e historial (contact_changes)."""

import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ContactChange, ContactField, ContactFieldValue, utcnow

FIELD_TYPES = ("text", "long_text", "number", "currency", "date", "datetime", "select", "boolean", "email",
               "phone")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TRUE = {"si", "sí", "true", "1", "yes", "verdadero", "x"}
FALSE = {"no", "false", "0", "falso"}

# Campos nativos del contacto que también se editan y auditan.
NATIVE_FIELDS = {"name": "Nombre", "email": "Email", "stage": "Etapa", "notes": "Notas", "memory": "Memoria"}


def parse_number(value: str) -> float | None:
    """Entiende "$ 80.000.000", "1.234,5", "1,234.5", "80000000" y "2,5"."""
    v = re.sub(r"[^\d,.\-]", "", value)
    if not re.search(r"\d", v):
        return None
    if "," in v and "." in v:  # el último separador es el decimal
        dec = "," if v.rfind(",") > v.rfind(".") else "."
        v = v.replace("." if dec == "," else ",", "").replace(dec, ".")
    else:
        sep = "," if "," in v else "." if "." in v else None
        if sep:
            parts = v.split(sep)
            # Varios separadores, o uno seguido de exactamente 3 dígitos: separador de miles
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                v = v.replace(sep, "")
            else:
                v = v.replace(sep, ".")
    try:
        return float(v)
    except ValueError:
        return None


def coerce(field: ContactField, raw) -> str | int | float | bool | None:
    """Convierte al tipo del campo; None o "" borran el valor. Lanza ValueError si no es válido."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    t = field.type
    if t == "boolean":
        if isinstance(raw, bool):
            return raw
        v = str(raw).strip().lower()
        if v in TRUE:
            return True
        if v in FALSE:
            return False
        raise ValueError(f"«{field.label}» debe ser sí o no")
    value = str(raw).strip()
    if t in ("number", "currency"):
        n = parse_number(value)
        if n is None:
            raise ValueError(f"«{field.label}» debe ser un número")
        return int(n) if n.is_integer() else n
    if t == "date":
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            raise ValueError(f"«{field.label}» debe ser una fecha AAAA-MM-DD") from None
    if t == "datetime":
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            raise ValueError(f"«{field.label}» debe ser una fecha y hora ISO (AAAA-MM-DDTHH:MM)") from None
    if t == "select":
        options = field.options or []
        match = next((o for o in options if o.lower() == value.lower()), None)
        if match is None:
            raise ValueError(f"«{field.label}» debe ser una de: {', '.join(options)}")
        return match
    if t == "email" and not EMAIL_RE.match(value):
        raise ValueError(f"«{field.label}» no es un email válido")
    if t == "phone":
        digits = re.sub(r"[^\d+]", "", value)
        if len(re.sub(r"\D", "", digits)) < 7:
            raise ValueError(f"«{field.label}» no es un teléfono válido")
        return digits
    return value[:5000 if t == "long_text" else 500]


def _str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "sí" if v else "no"
    return str(v)


async def fields_by_key(session: AsyncSession, org: int, include_archived: bool = False) -> dict[str, ContactField]:
    stmt = select(ContactField).where(ContactField.organization_id == org)
    if not include_archived:
        stmt = stmt.where(ContactField.archived_at.is_(None))
    rows = (await session.scalars(stmt.order_by(ContactField.position, ContactField.id))).all()
    return {f.key: f for f in rows}


def custom_values(contact: Contact) -> dict:
    """Valores de campos personalizados del contacto (requiere field_values cargado)."""
    return {v.field.key: v.value for v in contact.field_values if v.field.archived_at is None}


async def human_edited_keys(session: AsyncSession, contact_id: int) -> set[str]:
    """Campos cuyo último cambio lo hizo una persona (la IA no los sobrescribe)."""
    rows = (await session.execute(
        select(ContactChange.field_key, ContactChange.source)
        .where(ContactChange.contact_id == contact_id).order_by(ContactChange.id))).all()
    last: dict[str, str] = {}
    for key, source in rows:
        last[key] = source
    return {k for k, s in last.items() if s != "ai"}


def _audit(session: AsyncSession, contact: Contact, key: str, old, new, source: str,
           agent_id: int | None, conversation_id: int | None) -> None:
    session.add(ContactChange(organization_id=contact.organization_id, contact_id=contact.id, field_key=key,
                              old_value=_str(old), new_value=_str(new), source=source, agent_id=agent_id,
                              conversation_id=conversation_id))


async def set_custom(
    session: AsyncSession, contact: Contact, field: ContactField, value, source: str,
    agent_id: int | None = None, conversation_id: int | None = None,
) -> bool:
    """Guarda un valor ya validado (coerce) en su columna tipada y lo audita. True si cambió."""
    row = await session.get(ContactFieldValue, (contact.id, field.id))
    old = row.value if row else None
    if old == value:
        return False
    if value is None:
        if row:
            await session.delete(row)
    else:
        if not row:
            row = ContactFieldValue(contact_id=contact.id, field_id=field.id, source=source)
            session.add(row)
        row.value_text = row.value_number = row.value_date = row.value_bool = None
        if field.type in ("number", "currency"):
            row.value_number = value
        elif field.type == "date":
            row.value_date = date.fromisoformat(value)
        elif field.type == "boolean":
            row.value_bool = value
        else:
            row.value_text = value
        row.source, row.updated_by, row.updated_at = source, agent_id, utcnow()
    _audit(session, contact, field.key, old, value, source, agent_id, conversation_id)
    return True


def set_native(
    session: AsyncSession, contact: Contact, key: str, value, source: str,
    agent_id: int | None = None, conversation_id: int | None = None,
) -> bool:
    old = getattr(contact, key)
    if old == value:
        return False
    setattr(contact, key, value)
    if key == "memory":
        contact.memory_updated_at = utcnow()
    _audit(session, contact, key, old, value, source, agent_id, conversation_id)
    return True
