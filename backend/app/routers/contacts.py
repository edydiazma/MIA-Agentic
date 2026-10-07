import csv
import io
import re
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.scope import contact_clause, scope_for
from app.auth import current_agent
from app.permissions import require_permission
from app.db import get_session
from app.fields import NATIVE_FIELDS, coerce, fields_by_key, set_custom, set_native
from app.models import (
    Agent,
    Channel,
    Contact,
    ContactTag,
    Conversation,
    Flow,
    InteractionProduct,
    Tag,
    Typification,
    utcnow,
)
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
    clause = contact_clause(await scope_for(session, agent))  # alcance del supervisor / rol (app/scope.py)
    if clause is not None and not await session.scalar(select(Contact.id).where(Contact.id == c.id, clause)):
        raise HTTPException(404, "Contacto no encontrado")
    return c


async def _scoped(session: AsyncSession, agent: Agent, conds: list) -> list:
    clause = contact_clause(await scope_for(session, agent))
    return conds if clause is None else [*conds, clause]


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


# --- Lista de clientes (columnas, filtros, orden, exportación) — docs/data-model.md §14 ---------------------
SORTS = ("name", "created_at", "updated_at", "first_interaction_at", "last_interaction_at", "conversations_count",
         "messages_in", "products_count", "lifetime_days", "days_since_last_interaction", "first_source_at")
SOURCE_FIELDS = ("first_source_channel", "first_source_ad_id", "first_source_campaign", "first_source_label",
                 "first_source_at", "last_source_channel", "last_source_ad_id", "last_source_campaign",
                 "last_source_label", "last_source_at")


class ContactFilters:
    """Filtros compartidos por la lista, el conteo y la exportación CSV."""

    def __init__(
        self,
        q: str | None = None, tag: str | None = None, tags: str | None = None, stage: str | None = None,
        blocked: bool = False, channels: str | None = None, channel_ids: str | None = None,
        agent_id: int | None = None,
        typification_id: int | None = None, created_from: date | None = None, created_to: date | None = None,
        updated_from: date | None = None, updated_to: date | None = None, last_interaction_from: date | None = None,
        last_interaction_to: date | None = None, inactive_days_gte: float | None = None,
        has_products: bool | None = None, product: str | None = None, source_channel: str | None = None,
        source_ad_id: str | None = None, source_campaign: str | None = None,
        sort: str | None = Query(default=None, pattern="^(" + "|".join(SORTS) + ")$"),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ):
        self.__dict__.update({k: v for k, v in locals().items() if k != "self"})

    def where(self, org: int):
        def day_start(d: date) -> datetime:
            return datetime(d.year, d.month, d.day, tzinfo=UTC)

        conds = [Contact.organization_id == org, Contact.blocked == self.blocked]
        if self.q:
            like = f"%{self.q.strip().lstrip('@')}%"
            conds.append(or_(Contact.name.ilike(like), Contact.wa_id.ilike(like), Contact.email.ilike(like),
                             Contact.wa_username.ilike(like), Contact.wa_bsuid.ilike(like)))
        for name in [t for t in ([self.tag] if self.tag else []) + (self.tags or "").split(",") if t and t.strip()]:
            conds.append(exists().where(ContactTag.contact_id == Contact.id, ContactTag.tag_id == Tag.id,
                                        Tag.name == name.strip().lower(), Tag.organization_id == org))
        if self.stage:
            conds.append(Contact.stage == self.stage)
        # `channels`: tipos de canal (whatsapp_cloud, instagram…) o ids de canal; `channel_ids`: ids de canal
        tokens = [c.strip() for c in f"{self.channels or ''},{self.channel_ids or ''}".split(",") if c.strip()]
        providers = [t for t in tokens if not t.isdigit()]
        ids = [int(t) for t in tokens if t.isdigit()]
        if providers or ids:
            conds.append(or_(*([Contact.channel_providers.overlap(providers)] if providers else []),
                             *([Contact.channel_ids.overlap(ids)] if ids else [])))
        if self.agent_id:
            conds.append(Contact.last_agent_id == self.agent_id)
        if self.typification_id:
            conds.append(Contact.last_typification_id == self.typification_id)
        for col, lo, hi in ((Contact.created_at, self.created_from, self.created_to),
                            (Contact.updated_at, self.updated_from, self.updated_to),
                            (Contact.last_interaction_at, self.last_interaction_from, self.last_interaction_to)):
            if lo:
                conds.append(col >= day_start(lo))
            if hi:
                conds.append(col < day_start(hi) + timedelta(days=1))
        if self.inactive_days_gte is not None:
            conds.append(Contact.last_interaction_at <= utcnow() - timedelta(days=self.inactive_days_gte))
        if self.has_products is not None:
            conds.append(Contact.products_count > 0 if self.has_products else Contact.products_count == 0)
        if self.source_channel:
            conds.append(Contact.first_source_channel == self.source_channel.strip())
        if self.source_ad_id:
            conds.append(Contact.first_source_ad_id == self.source_ad_id.strip())
        if self.source_campaign:
            conds.append(Contact.first_source_campaign.ilike(f"%{self.source_campaign.strip()}%"))
        if self.product:
            p = self.product.strip()
            match = [InteractionProduct.external_ref == p, InteractionProduct.name.ilike(f"%{p}%")]
            if p.isdigit():
                match.append(InteractionProduct.product_id == int(p))
            conds.append(exists().where(InteractionProduct.contact_id == Contact.id, or_(*match)))
        return conds

    def order_by(self):
        if not self.sort:
            first = Contact.blocked_at.desc() if self.blocked else Contact.created_at.desc()
            return [first, Contact.id.desc()]
        asc = self.order == "asc"
        if self.sort == "lifetime_days":
            expr = Contact.last_interaction_at - Contact.first_interaction_at
        elif self.sort == "days_since_last_interaction":
            expr, asc = Contact.last_interaction_at, not asc  # más días = interacción más antigua
        else:
            expr = getattr(Contact, self.sort)
        return [(expr.asc() if asc else expr.desc()).nulls_last(), Contact.id.desc()]


async def _refs(session: AsyncSession, contacts: list[Contact]) -> dict[int, dict]:
    """Nombres de último asesor, tipificación y flujo (3 consultas para toda la página, sin N+1)."""
    async def names(model, ids):
        ids = {i for i in ids if i}
        if not ids:
            return {}
        return dict((await session.execute(select(model.id, model.name).where(model.id.in_(ids)))).all())

    agents = await names(Agent, [c.last_agent_id for c in contacts])
    typs = await names(Typification, [c.last_typification_id for c in contacts])
    flows = await names(Flow, [c.last_flow_id for c in contacts])
    channel_ids = {i for c in contacts for i in (c.channel_ids or [])}
    channels = {ch.id: {"id": ch.id, "name": ch.name, "provider": ch.provider, "display_phone": ch.display_phone}
                for ch in (await session.execute(select(Channel.id, Channel.name, Channel.provider,
                                                        Channel.display_phone)
                                                 .where(Channel.id.in_(channel_ids)))).all()} if channel_ids else {}

    def ref(table, key):
        return {"id": key, "name": table[key]} if key in table else None

    return {c.id: {"last_agent": ref(agents, c.last_agent_id), "last_typification": ref(typs, c.last_typification_id),
                   "last_flow": ref(flows, c.last_flow_id),
                   "channels": [channels[i] for i in (c.channel_ids or []) if i in channels]} for c in contacts}


async def contact_rows(session: AsyncSession, contacts: list[Contact]) -> list[dict]:
    refs = await _refs(session, contacts)
    return [{**contact_out(c), **refs[c.id], **{f: _iso(getattr(c, f)) for f in SOURCE_FIELDS}} for c in contacts]


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


@router.get("")
async def list_contacts(
    f: ContactFilters = Depends(), offset: int = 0, limit: int = Query(default=50, le=500),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    org = agent.organization_id
    conds = await _scoped(session, agent, f.where(org))
    total = await session.scalar(select(func.count(Contact.id)).where(*conds))
    rows = (await session.scalars(select(Contact).where(*conds).order_by(*f.order_by())
                                  .offset(offset).limit(limit))).all()
    return {"total": total, "items": await contact_rows(session, list(rows))}


BASE_COLUMNS = [
    ("name", "Nombre completo", "text", True, True, "Básico"),
    ("wa_id", "Teléfono", "text", False, True, "Básico"),
    ("wa_username", "Usuario de WhatsApp", "text", False, True, "Básico"),
    ("wa_bsuid", "ID de WhatsApp (BSUID)", "text", False, False, "Básico"),
    ("email", "Correo", "text", False, False, "Básico"),
    ("tags", "Etiquetas", "tags", False, True, "Básico"),
    ("channels", "Canales", "channels", False, True, "Básico"),
    ("channel_providers", "Tipos de canal", "channels", False, False, "Básico"),
    ("last_agent", "Agente", "ref", False, True, "Básico"),
    ("last_typification", "Tipificación", "ref", False, True, "Básico"),
    ("stage", "Etapa", "text", False, False, "Básico"),
    ("created_at", "F. Creación", "date", True, True, "Básico"),
    ("updated_at", "F. Actualización", "date", True, True, "Básico"),
    ("first_interaction_at", "Primera interacción", "date", True, True, "Interacción"),
    ("last_interaction_at", "Última interacción", "date", True, True, "Interacción"),
    ("first_inbound_at", "Primer mensaje del cliente", "date", False, False, "Interacción"),
    ("last_inbound_at", "Último mensaje del cliente", "date", False, False, "Interacción"),
    ("last_outbound_at", "Último mensaje enviado", "date", False, False, "Interacción"),
    ("lifetime_days", "Días entre primera y última", "number", True, False, "Interacción"),
    ("days_since_last_interaction", "Días sin interacción", "number", True, True, "Interacción"),
    ("conversations_count", "Conversaciones", "number", True, False, "Interacción"),
    ("messages_in", "Mensajes recibidos", "number", True, False, "Interacción"),
    ("messages_out", "Mensajes enviados", "number", False, False, "Interacción"),
    ("flow_runs_count", "Flujos ejecutados", "number", False, False, "Interacción"),
    ("last_flow", "Último flujo", "ref", False, False, "Interacción"),
    ("last_flow_at", "Fecha último flujo", "date", False, False, "Interacción"),
    ("products_count", "Productos", "number", True, False, "Productos"),
    ("last_product_name", "Último producto", "text", False, True, "Productos"),
    ("first_source_label", "Fuente (primer toque)", "text", False, True, "Fuente"),
    ("last_source_label", "Última fuente", "text", False, False, "Fuente"),
    ("first_source_campaign", "Campaña de origen", "text", False, False, "Fuente"),
    ("first_source_ad_id", "Anuncio de origen", "text", False, False, "Fuente"),
    ("first_source_at", "Fecha de la fuente", "date", True, False, "Fuente"),
]


async def _columns(session: AsyncSession, org: int) -> list[dict]:
    cols = [{"key": k, "label": label, "type": t, "sortable": s, "default_visible": v, "group": g}
            for k, label, t, s, v, g in BASE_COLUMNS]
    for fdef in (await fields_by_key(session, org)).values():
        cols.append({"key": f"custom:{fdef.key}", "label": fdef.label,
                     "type": {"number": "number", "date": "date"}.get(fdef.type, "text"), "sortable": False,
                     "default_visible": False, "group": "Campos personalizados"})
    return cols


@router.get("/columns")
async def list_columns(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await _columns(session, agent.organization_id)


def _cell(row: dict, key: str) -> str:
    if key.startswith("custom:"):
        value = (row.get("custom_fields") or {}).get(key.split(":", 1)[1])
    else:
        value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, list):
        return ", ".join(str(v.get("display_phone") or v.get("name")) if isinstance(v, dict) else str(v)
                         for v in value)
    if isinstance(value, bool):
        return "Sí" if value else "No"
    return str(value)


@router.get("/export.csv")
async def export_contacts(
    f: ContactFilters = Depends(), columns: str | None = None,
    agent: Agent = Depends(require_permission("exports.contacts")), session: AsyncSession = Depends(get_session),
):
    org = agent.organization_id
    available = {c["key"]: c["label"] for c in await _columns(session, org)}
    keys = [k for k in (columns or "").split(",") if k in available] or \
        [k for k, *_rest, visible, _g in BASE_COLUMNS if visible]
    conds, order = await _scoped(session, agent, f.where(org)), f.order_by()

    async def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        buf.write("\ufeff")  # BOM: Excel abre bien las tildes
        writer.writerow([available[k] for k in keys])
        yield buf.getvalue()
        offset = 0
        while True:
            batch = (await session.scalars(select(Contact).where(*conds).order_by(*order)
                                           .offset(offset).limit(500))).all()
            if not batch:
                break
            buf = io.StringIO()
            writer = csv.writer(buf)
            for row in await contact_rows(session, list(batch)):
                writer.writerow([_cell(row, k) for k in keys])
            yield buf.getvalue()
            offset += len(batch)

    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="clientes.csv"'})


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
    from app.golden.fields import field_index, resolve_field, write_value

    index = await field_index(session, org)  # clave, etiqueta o alias (Atom, otros sistemas)
    by_header = {}
    for header in cols.values():
        if header in (phone_col, name_col, email_col):
            continue
        f = await resolve_field(session, org, header, index)
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
            try:  # cada valor va a su destino: registro maestro, vehículo, consentimiento, oportunidad o ficha
                await write_value(session, contact, f, row.get(header), "import")
            except ValueError:
                field_errors += 1
        await session.flush()
    await session.commit()
    return {"created": created, "updated": updated, "invalid": invalid, "field_errors": field_errors,
            "custom_columns": [f.label for f in by_header.values()]}


# --- Habeas data: exportar / eliminar los datos de un cliente (docs/ops/privacy.md) -------------------------------
@router.get("/{contact_id}/export")
async def export_contact_data(contact_id: int, agent: Agent = Depends(require_permission("contacts.privacy")),
                              session: AsyncSession = Depends(get_session)):
    """Todos los datos del cliente en JSON (solicitud de acceso del titular)."""
    from app.privacy import export_contact

    contact = await _scoped_contact(session, agent, contact_id)
    data = await export_contact(session, contact)
    return JSONResponse(data, headers={
        "Content-Disposition": f'attachment; filename="cliente-{contact_id}-datos.json"'})


class EraseIn(BaseModel):
    confirm: str  # debe ser "ELIMINAR": la operación es irreversible


@router.delete("/{contact_id}/erase")
async def erase_contact_data(contact_id: int, body: EraseIn, agent: Agent = Depends(require_permission("contacts.privacy")),
                             session: AsyncSession = Depends(get_session)):
    """Anonimiza al cliente de forma irreversible (solicitud de supresión del titular)."""
    from app.privacy import erase_contact

    if body.confirm != "ELIMINAR":
        raise HTTPException(422, "Escribe ELIMINAR para confirmar: esta acción no se puede deshacer")
    contact = await _scoped_contact(session, agent, contact_id)
    stats = await erase_contact(session, contact, agent.id)
    await session.commit()
    return {"ok": True, "contact_id": contact_id, "erased": stats}


async def _scoped_contact(session: AsyncSession, agent: Agent, contact_id: int) -> Contact:
    contact = await session.get(Contact, contact_id)
    if not contact or contact.organization_id != agent.organization_id:
        raise HTTPException(404, "Cliente no encontrado")
    clause = contact_clause(await scope_for(session, agent))
    if clause is not None and await session.scalar(select(Contact.id).where(Contact.id == contact_id, clause)) is None:
        raise HTTPException(404, "Cliente no encontrado")
    return contact


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
    await hub.broadcast("contact.updated", out, contact.organization_id)
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
