import csv
import io
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.db import get_session
from app.fields import NATIVE_FIELDS, coerce, fields_by_key, set_custom, set_native
from app.models import Agent, Contact, ContactTag, Conversation, Tag, utcnow
from app.realtime import hub
from app.schemas import ContactUpdate
from app.service import close, contact_out, conversation_out, tag_by_name, typification_by_name

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

STAGES = ("lead", "prospect", "client", "lost")
PHONE_COLS = ("telefono", "teléfono", "phone", "celular", "whatsapp", "numero", "número")


class ContactCreate(BaseModel):
    wa_id: str
    name: str | None = None
    email: str | None = None
    tags: list[str] = []


class BlockIn(BaseModel):
    reason: str | None = None


def normalize_phone(raw: str) -> str | None:
    """WhatsApp usa el número en formato internacional sin '+' ni espacios."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if 8 <= len(digits) <= 15 else None


async def _contact(session: AsyncSession, contact_id: int, agent: Agent) -> Contact:
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Contacto no encontrado")
    return c


async def _reload(session: AsyncSession, c: Contact) -> Contact:
    await session.refresh(c)
    await session.refresh(c, ["field_values", "tag_links"])
    return c


async def set_contact_tags(session: AsyncSession, contact: Contact, names: list[str], source: str,
                           replace: bool = True) -> None:
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    current = {link.tag.name: link for link in contact.tag_links}
    for name in wanted - current.keys():
        tag = await tag_by_name(session, contact.organization_id, name)
        session.add(ContactTag(contact_id=contact.id, tag_id=tag.id, source=source))
    if replace:
        for name in current.keys() - wanted:
            await session.execute(delete(ContactTag).where(ContactTag.contact_id == contact.id,
                                                           ContactTag.tag_id == current[name].tag_id))


@router.get("")
async def list_contacts(
    q: str | None = None, tag: str | None = None, stage: str | None = None, blocked: bool = False,
    offset: int = 0, limit: int = Query(default=50, le=500),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    org = agent.organization_id
    stmt = select(Contact).where(Contact.organization_id == org, Contact.blocked == blocked)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Contact.name.ilike(like), Contact.wa_id.ilike(like), Contact.email.ilike(like)))
    if tag:
        stmt = stmt.where(exists().where(ContactTag.contact_id == Contact.id, ContactTag.tag_id == Tag.id,
                                         Tag.name == tag.strip().lower(), Tag.organization_id == org))
    if stage:
        stmt = stmt.where(Contact.stage == stage)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    order = Contact.blocked_at.desc() if blocked else Contact.created_at.desc()
    rows = (await session.scalars(stmt.order_by(order, Contact.id.desc()).offset(offset).limit(limit))).all()
    return {"total": total, "items": [contact_out(c) for c in rows]}


@router.get("/tags")
async def list_tags(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Tag.name, func.count(ContactTag.contact_id))
        .join(ContactTag, ContactTag.tag_id == Tag.id)
        .where(Tag.organization_id == agent.organization_id)
        .group_by(Tag.name).order_by(Tag.name))).all()
    return [{"tag": t, "count": n} for t, n in rows]


@router.post("")
async def create_contact(body: ContactCreate, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    wa_id = normalize_phone(body.wa_id)
    if not wa_id:
        raise HTTPException(422, "Número inválido: usa el formato internacional, p. ej. 573001234567")
    org = agent.organization_id
    if await session.scalar(select(Contact.id).where(Contact.organization_id == org, Contact.wa_id == wa_id)):
        raise HTTPException(409, "El contacto ya existe")
    contact = Contact(organization_id=org, wa_id=wa_id, name=body.name, email=body.email)
    session.add(contact)
    await session.flush()
    await _reload(session, contact)
    await set_contact_tags(session, contact, body.tags, "agent")
    await session.commit()
    return contact_out(await _reload(session, contact))


@router.post("/import")
async def import_contacts(
    file: UploadFile = File(...), tags: str = Form(default=""),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    """CSV con columnas telefono (obligatoria), nombre, email y campos personalizados (por clave o nombre)."""
    org = agent.organization_id
    text = (await file.read()).decode("utf-8-sig", "replace")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t") if text.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
    phone_col = next((cols[k] for k in PHONE_COLS if k in cols), None)
    if not phone_col:
        raise HTTPException(422, "El CSV debe tener una columna 'telefono'")
    name_col = next((cols[k] for k in ("nombre", "name") if k in cols), None)
    email_col = next((cols[k] for k in ("email", "correo") if k in cols), None)
    extra = [t for t in tags.split(",") if t.strip()]
    defs = await fields_by_key(session, org)
    by_header = {}
    for header_key, header in cols.items():
        f = defs.get(header_key) or next((d for d in defs.values() if d.label.lower() == header_key), None)
        if f:
            by_header[header] = f

    created = updated = invalid = field_errors = 0
    for row in reader:
        wa_id = normalize_phone(row.get(phone_col, ""))
        if not wa_id:
            invalid += 1
            continue
        contact = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_id == wa_id))
        if not contact:
            contact = Contact(organization_id=org, wa_id=wa_id)
            session.add(contact)
            await session.flush()
            created += 1
        else:
            updated += 1
        await session.refresh(contact, ["field_values", "tag_links"])
        if name_col and row.get(name_col):
            contact.name = contact.name or row[name_col].strip()
        if email_col and row.get(email_col):
            contact.email = row[email_col].strip()
        if extra:
            await set_contact_tags(session, contact, extra, "import", replace=False)
        for header, f in by_header.items():
            try:
                value = coerce(f, row.get(header))
            except ValueError:
                field_errors += 1
                continue
            if value is not None:
                await set_custom(session, contact, f, value, "import")
        await session.flush()
    await session.commit()
    return {"created": created, "updated": updated, "invalid": invalid, "field_errors": field_errors,
            "custom_columns": [f.label for f in by_header.values()]}


@router.get("/{contact_id}")
async def get_contact(contact_id: int, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    convs = (await session.scalars(
        select(Conversation).where(Conversation.contact_id == contact_id).order_by(Conversation.id.desc())
    )).unique().all()
    return {**contact_out(contact), "conversations": [conversation_out(c) for c in convs]}


@router.put("/{contact_id}")
async def update_contact(
    contact_id: int, body: ContactUpdate, agent: Agent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
    conversation_id: int | None = None,  # desde qué conversación se editó (para el historial)
):
    """El usuario que inició sesión edita la ficha; cada cambio queda en contact_changes con su nombre."""
    contact = await _contact(session, contact_id, agent)
    data = body.model_dump(exclude_unset=True)
    if "stage" in data and data["stage"] not in STAGES:
        raise HTTPException(422, f"Etapa inválida: {', '.join(STAGES)}")
    if "tags" in data:
        await set_contact_tags(session, contact, data.pop("tags") or [], "agent")
    custom = data.pop("custom_fields", None) or {}
    for k, v in data.items():
        if k in NATIVE_FIELDS:
            set_native(session, contact, k, (v.strip() or None) if isinstance(v, str) else v, "agent",
                       agent_id=agent.id, conversation_id=conversation_id)
    if custom:
        defs = await fields_by_key(session, agent.organization_id)
        errors = []
        for key, raw in custom.items():
            field = defs.get(key)
            if not field:
                errors.append(f"Campo desconocido: {key}")
                continue
            if not field.agent_editable and agent.role != "admin":
                errors.append(f"«{field.label}» solo lo puede editar un administrador")
                continue
            try:
                await set_custom(session, contact, field, coerce(field, raw), "agent",
                                 agent_id=agent.id, conversation_id=conversation_id)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            await session.rollback()
            raise HTTPException(422, "; ".join(errors))
    await session.commit()
    out = contact_out(await _reload(session, contact))
    await hub.broadcast("contact.updated", out)
    return out


@router.post("/{contact_id}/block")
async def block(contact_id: int, body: BlockIn, agent: Agent = Depends(current_agent),
                session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    contact.blocked, contact.blocked_reason, contact.blocked_at = True, body.reason, utcnow()
    await session.commit()
    spam = await typification_by_name(session, agent.organization_id, "Spam")
    for conv in (await session.scalars(select(Conversation).where(
            Conversation.contact_id == contact_id, Conversation.status != "closed"))).unique().all():
        await close(session, conv, spam, actor="agent", agent_id=agent.id)
    return contact_out(await _reload(session, contact))


@router.post("/{contact_id}/unblock")
async def unblock(contact_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, contact_id, agent)
    contact.blocked, contact.blocked_reason, contact.blocked_at = False, None, None
    await session.commit()
    return contact_out(await _reload(session, contact))
