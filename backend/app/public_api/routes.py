"""Endpoints de la API pública /v1 (docs/api.md). Todo se filtra por la empresa de la llave."""

import secrets
from datetime import date

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, set_actor
from app.models import (
    Agent,
    Contact,
    ContactTag,
    Conversation,
    Deal,
    Group,
    Message,
    Organization,
    OutboundWebhook,
    Tag,
)
from app.public_api.core import ApiContext, ApiError, Idempotency, api_context, decode_cursor, idempotency, page, require
from app.public_api.samples import EVENT_DESCRIPTIONS, SAMPLES

router = APIRouter()
CONNECTORS = ("api", "zapier", "make", "n8n")


# --- Serializadores (forma estable de la API, independiente del panel) ----------------
def contact_json(c: Contact) -> dict:
    from app.fields import custom_values

    return {"id": c.id, "phone": c.wa_id, "name": c.name, "email": c.email, "stage": c.stage, "notes": c.notes,
            "tags": sorted(link.tag.name for link in c.tag_links), "custom_fields": custom_values(c),
            "marketing_opt_out": c.marketing_opt_out, "blocked": c.blocked,
            "created_at": c.created_at, "updated_at": c.updated_at}


def conversation_json(c: Conversation) -> dict:
    return {"id": c.id, "status": c.status, "channel": {"id": c.channel_id, "provider": c.channel.provider},
            "contact": {"id": c.contact.id, "name": c.contact.name, "phone": c.contact.wa_id},
            "assigned_agent": {"id": c.assigned_agent.id, "name": c.assigned_agent.name} if c.assigned_agent else None,
            "group": {"id": c.group.id, "name": c.group.name} if c.group else None,
            "typification": c.typification.name if c.typification else None,
            "tags": sorted(link.tag.name for link in c.tag_links), "unread_count": c.unread_count,
            "message_count": c.message_count, "last_message_at": c.last_message_at,
            "last_inbound_at": c.last_inbound_at, "closed_at": c.closed_at, "created_at": c.created_at}


def message_json(m: Message) -> dict:
    return {"id": m.id, "conversation_id": m.conversation_id, "direction": m.direction, "sender_type": m.sender_type,
            "sender_agent_id": m.sender_agent_id, "type": m.type, "text": m.text, "template_name": m.template_name,
            "has_media": bool(m.media_path), "media_mime": m.media_mime, "status": m.status, "error": m.error,
            "created_at": m.created_at}


async def deal_json(session: AsyncSession, d: Deal) -> dict:
    await session.refresh(d, ["contact", "owner"])
    return {"id": d.id, "name": d.name, "contact": {"id": d.contact.id, "name": d.contact.name, "phone": d.contact.wa_id},
            "conversation_id": d.conversation_id, "owner": {"id": d.owner.id, "name": d.owner.name} if d.owner else None,
            "amount": float(d.amount) if d.amount is not None else None, "currency": d.currency, "pipeline": d.pipeline,
            "stage": d.stage, "status": d.status, "source": d.source, "expected_close": d.expected_close,
            "closed_at": d.closed_at, "lost_reason": d.lost_reason, "created_at": d.created_at,
            "updated_at": d.updated_at}


def _limit(limit: int) -> int:
    return max(1, min(limit, 100))


# --- Cuenta -------------------------------------------------------------------------
@router.get("/me", summary="Empresa y llave (prueba de conexión para Zapier, Make y n8n)")
async def me(ctx: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    org = await session.get(Organization, ctx.org)
    return {"organization": {"id": org.id, "name": org.name, "timezone": org.timezone},
            "key": {"id": ctx.key.id, "name": ctx.key.name, "prefix": ctx.key.prefix, "scopes": sorted(ctx.scopes),
                    "rate_limit_per_min": ctx.key.rate_limit_per_min}}


# --- Contactos ------------------------------------------------------------------------
class ContactIn(BaseModel):
    phone: str | None = Field(default=None, description="Formato internacional, p. ej. 573001234567")
    email: str | None = None
    name: str | None = None
    stage: str | None = Field(default=None, description="lead | prospect | client | lost")
    notes: str | None = None
    tags: list[str] | None = Field(default=None, description="Se agregan a las existentes")
    custom_fields: dict | None = Field(default=None, description="{clave_del_campo: valor}")


class ContactPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    stage: str | None = None
    notes: str | None = None
    tags: list[str] | None = Field(default=None, description="Reemplaza todas las etiquetas")
    custom_fields: dict | None = None


class TagsIn(BaseModel):
    add: list[str] = []
    remove: list[str] = []


async def _contact(session: AsyncSession, org: int, contact_id: int) -> Contact:
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != org:
        raise ApiError(404, "Contacto no encontrado")
    return c


async def _apply_contact(session: AsyncSession, contact: Contact, data: dict, replace_tags: bool) -> None:
    from app.fields import coerce, fields_by_key, set_custom, set_native
    from app.routers.contacts import STAGES, set_contact_tags

    if data.get("stage") is not None and data["stage"] not in STAGES:
        raise ApiError(422, f"Etapa inválida: {', '.join(STAGES)}")
    for k in ("name", "email", "stage", "notes"):
        if k in data and data[k] is not None:
            v = data[k].strip() if isinstance(data[k], str) else data[k]
            set_native(session, contact, k, v or None, "api")
    if data.get("tags") is not None:
        await session.refresh(contact, ["tag_links"])
        await set_contact_tags(session, contact, data["tags"], "api", replace=replace_tags)
    if data.get("custom_fields"):
        defs = await fields_by_key(session, contact.organization_id)
        errors = []
        for key, raw in data["custom_fields"].items():
            field = defs.get(key)
            if not field:
                errors.append(f"Campo desconocido: {key}")
                continue
            try:
                await set_custom(session, contact, field, coerce(field, raw), "api")
            except ValueError as e:
                errors.append(f"{key}: {e}")
        if errors:
            await session.rollback()
            raise ApiError(422, "; ".join(errors))


async def _reloaded(session: AsyncSession, c: Contact) -> dict:
    await session.refresh(c)
    await session.refresh(c, ["field_values", "tag_links"])
    return contact_json(c)


@router.get("/contacts", summary="Listar o buscar contactos")
async def list_contacts(q: str | None = None, tag: str | None = None, stage: str | None = None,
                        updated_since: str | None = None, limit: int = 50, cursor: str | None = None,
                        ctx: ApiContext = Depends(require("contacts:read")),
                        session: AsyncSession = Depends(get_session)):
    limit = _limit(limit)
    stmt = select(Contact).where(Contact.organization_id == ctx.org)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Contact.name.ilike(like), Contact.wa_id.ilike(like), Contact.email.ilike(like)))
    if stage:
        stmt = stmt.where(Contact.stage == stage)
    if tag:
        stmt = stmt.where(Contact.id.in_(select(ContactTag.contact_id).join(Tag, Tag.id == ContactTag.tag_id)
                                         .where(Tag.organization_id == ctx.org, Tag.name == tag.strip().lower())))
    if updated_since:
        stmt = stmt.where(Contact.updated_at >= updated_since)
    after = decode_cursor(cursor)
    if after:
        stmt = stmt.where(Contact.id < after)
    rows = (await session.scalars(stmt.order_by(Contact.id.desc()).limit(limit + 1))).all()
    out = page(list(rows), limit, lambda c: c.id)
    out["data"] = [contact_json(c) for c in out["data"]]
    return out


@router.get("/contacts/{contact_id}", summary="Obtener un contacto")
async def get_contact(contact_id: int, ctx: ApiContext = Depends(require("contacts:read")),
                      session: AsyncSession = Depends(get_session)):
    return contact_json(await _contact(session, ctx.org, contact_id))


@router.post("/contacts", summary="Crear o actualizar (por teléfono o correo)", status_code=200)
async def upsert_contact(body: ContactIn, idem: Idempotency = Depends(idempotency("contacts:write")),
                         session: AsyncSession = Depends(get_session)):
    """Busca por `phone` y, si no hay, por `email`. Si existe lo actualiza (200); si no, lo crea (201)."""
    if idem.replay:
        return idem.replay
    from app.routers.contacts import normalize_phone
    from app.service import get_or_create_contact

    org = idem.org
    phone = normalize_phone(body.phone) if body.phone else None
    if body.phone and not phone:
        raise ApiError(422, "Teléfono inválido: usa el formato internacional, p. ej. 573001234567")
    if not phone and not (body.email or "").strip():
        raise ApiError(422, "Indica `phone` o `email`")
    contact = None
    if phone:
        contact = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_id == phone))
    if contact is None and body.email:
        contact = await session.scalar(select(Contact).where(
            Contact.organization_id == org, func.lower(Contact.email) == body.email.strip().lower())
            .order_by(Contact.id).limit(1))
    created = contact is None
    if created:
        if phone:
            contact, _ = await get_or_create_contact(session, org, phone, body.name)
        else:
            contact = Contact(organization_id=org, name=body.name)
            session.add(contact)
            await session.flush()
        await session.refresh(contact, ["field_values", "tag_links"])
    elif phone and not contact.wa_id:
        contact.wa_id = phone
    await _apply_contact(session, contact, body.model_dump(), replace_tags=False)
    await session.commit()
    return await idem.respond({"created": created, "contact": await _reloaded(session, contact)}, 201 if created else 200)


@router.patch("/contacts/{contact_id}", summary="Actualizar un contacto")
async def patch_contact(contact_id: int, body: ContactPatch, ctx: ApiContext = Depends(require("contacts:write")),
                        session: AsyncSession = Depends(get_session)):
    contact = await _contact(session, ctx.org, contact_id)
    await _apply_contact(session, contact, body.model_dump(exclude_unset=True), replace_tags=True)
    await session.commit()
    return await _reloaded(session, contact)


@router.post("/contacts/{contact_id}/tags", summary="Agregar o quitar etiquetas")
async def contact_tags(contact_id: int, body: TagsIn, ctx: ApiContext = Depends(require("contacts:write")),
                       session: AsyncSession = Depends(get_session)):
    from app.routers.contacts import set_contact_tags

    contact = await _contact(session, ctx.org, contact_id)
    current = {link.tag.name for link in contact.tag_links}
    remove = {t.strip().lower() for t in body.remove}
    wanted = (current | {t.strip().lower() for t in body.add if t.strip()}) - remove
    await set_contact_tags(session, contact, sorted(wanted), "api", replace=True)
    await session.commit()
    return await _reloaded(session, contact)


# --- Conversaciones y mensajes ---------------------------------------------------------
class AssignIn(BaseModel):
    agent_id: int | None = Field(default=None, description="null = sin asesor (queda en la cola del grupo)")
    group_id: int | None = None


class CloseIn(BaseModel):
    typification: str | None = Field(default=None, description="Nombre de la tipificación")


class TemplateRef(BaseModel):
    name: str
    language: str
    values: list[str] = []


class SendIn(BaseModel):
    text: str | None = None
    template: TemplateRef | None = Field(default=None, description="Solo WhatsApp: necesaria fuera de la ventana de 24 h")
    agent_id: int | None = Field(default=None, description="Asesor en cuyo nombre se envía (opcional)")


class SendToPhoneIn(SendIn):
    phone: str
    channel_id: int | None = Field(default=None, description="Número de WhatsApp de la empresa (por defecto el primero)")
    name: str | None = None


async def _conversation(session: AsyncSession, org: int, conv_id: int) -> Conversation:
    from app.service import get_conversation

    conv = await get_conversation(session, conv_id, org)
    if not conv:
        raise ApiError(404, "Conversación no encontrada")
    return conv


async def _agent(session: AsyncSession, org: int, agent_id: int | None) -> Agent | None:
    if agent_id is None:
        return None
    a = await session.get(Agent, agent_id)
    if not a or a.organization_id != org or not a.is_active:
        raise ApiError(422, "Asesor inválido")
    return a


@router.get("/conversations", summary="Listar conversaciones")
async def list_conversations(status: str | None = Query(default=None, pattern="^(bot|human|closed|open)$"),
                             contact_id: int | None = None, assigned_agent_id: int | None = None,
                             group_id: int | None = None, channel_id: int | None = None,
                             updated_since: str | None = None, limit: int = 50, cursor: str | None = None,
                             ctx: ApiContext = Depends(require("conversations:read")),
                             session: AsyncSession = Depends(get_session)):
    limit = _limit(limit)
    stmt = select(Conversation).where(Conversation.organization_id == ctx.org)
    if status == "open":
        stmt = stmt.where(Conversation.status != "closed")
    elif status:
        stmt = stmt.where(Conversation.status == status)
    for col, value in ((Conversation.contact_id, contact_id), (Conversation.assigned_agent_id, assigned_agent_id),
                       (Conversation.group_id, group_id), (Conversation.channel_id, channel_id)):
        if value is not None:
            stmt = stmt.where(col == value)
    if updated_since:
        stmt = stmt.where(Conversation.updated_at >= updated_since)
    after = decode_cursor(cursor)
    if after:
        stmt = stmt.where(Conversation.id < after)
    rows = (await session.scalars(stmt.order_by(Conversation.id.desc()).limit(limit + 1))).unique().all()
    out = page(list(rows), limit, lambda c: c.id)
    out["data"] = [conversation_json(c) for c in out["data"]]
    return out


@router.get("/conversations/{conv_id}", summary="Obtener una conversación con sus últimos mensajes")
async def get_conversation(conv_id: int, messages: int = 20, ctx: ApiContext = Depends(require("conversations:read")),
                           session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, ctx.org, conv_id)
    out = conversation_json(conv)
    if "messages:read" in ctx.scopes:
        rows = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                      .order_by(Message.created_at.desc()).limit(max(0, min(messages, 100))))).all()
        out["messages"] = [message_json(m) for m in reversed(rows)]
    return out


@router.get("/conversations/{conv_id}/messages", summary="Mensajes de una conversación (del más nuevo al más viejo)")
async def list_messages(conv_id: int, limit: int = 50, cursor: str | None = None,
                        ctx: ApiContext = Depends(require("messages:read")), session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, ctx.org, conv_id)
    limit = _limit(limit)
    stmt = select(Message).where(Message.conversation_id == conv.id)
    after = decode_cursor(cursor)
    if after:
        stmt = stmt.where(Message.id < after)
    rows = (await session.scalars(stmt.order_by(Message.id.desc()).limit(limit + 1))).all()
    out = page(list(rows), limit, lambda m: m.id)
    out["data"] = [message_json(m) for m in out["data"]]
    return out


async def _send(session: AsyncSession, conv: Conversation, body: SendIn) -> dict:
    from app.service import send_text, within_session_window

    agent = await _agent(session, conv.organization_id, body.agent_id)
    if body.template is not None:
        if conv.channel.provider != "whatsapp_cloud":
            raise ApiError(422, "Las plantillas solo existen en WhatsApp")
        from app import templates
        from app.campaigns import send_template_message

        catalog = await templates.list_templates(session, conv.channel)
        tpl = next((t for t in catalog if t["name"] == body.template.name and t["language"] == body.template.language),
                   None)
        if not tpl or tpl["status"] != "APPROVED":
            raise ApiError(404, "Plantilla no encontrada o no aprobada")
        if not tpl["supported"]:
            raise ApiError(422, tpl["unsupported_reason"])
        if tpl["category"] == "MARKETING" and conv.contact.marketing_opt_out:
            raise ApiError(409, "El contacto pidió no recibir marketing: usa una plantilla de utilidad", code="opted_out")
        try:
            templates.build(tpl, body.template.values)
        except ValueError as e:
            raise ApiError(422, str(e)) from e
        await set_actor(session, "api")
        msg = await send_template_message(session, conv, tpl, body.template.values, sender_type="agent",
                                          agent_id=agent.id if agent else None)
        return message_json(msg)
    text_ = (body.text or "").strip()
    if not text_:
        raise ApiError(422, "Indica `text` o `template`")
    if not within_session_window(conv):
        raise ApiError(409, "Fuera de la ventana de mensajes libres: en WhatsApp envía una plantilla (`template`)",
                       code="outside_window")
    await set_actor(session, "api")
    sent = await send_text(session, conv, text_, sender_type="agent", agent_id=agent.id if agent else None)
    return message_json(sent[0])


@router.post("/conversations/{conv_id}/messages", summary="Enviar un mensaje en una conversación", status_code=201)
async def send_in_conversation(conv_id: int, body: SendIn, idem: Idempotency = Depends(idempotency("messages:send")),
                               session: AsyncSession = Depends(get_session)):
    if idem.replay:
        return idem.replay
    conv = await _conversation(session, idem.org, conv_id)
    return await idem.respond(await _send(session, conv, body), 201)


@router.post("/messages", summary="Enviar un mensaje de WhatsApp a un teléfono (crea contacto y conversación)",
             status_code=201)
async def send_to_phone(body: SendToPhoneIn, idem: Idempotency = Depends(idempotency("messages:send")),
                        session: AsyncSession = Depends(get_session)):
    if idem.replay:
        return idem.replay
    from app.models import Channel
    from app.routers.contacts import normalize_phone
    from app.service import get_or_create_contact, get_or_create_conversation

    org = idem.org
    phone = normalize_phone(body.phone)
    if not phone:
        raise ApiError(422, "Teléfono inválido: usa el formato internacional, p. ej. 573001234567")
    stmt = select(Channel).where(Channel.organization_id == org, Channel.provider == "whatsapp_cloud")
    if body.channel_id is not None:
        stmt = stmt.where(Channel.id == body.channel_id)
    channel = (await session.scalars(stmt.order_by(Channel.id).limit(1))).first()
    if not channel:
        raise ApiError(404 if body.channel_id else 409, "No hay un número de WhatsApp conectado")
    contact, _ = await get_or_create_contact(session, org, phone, body.name)
    if contact.blocked:
        raise ApiError(409, "El contacto está bloqueado", code="contact_blocked")
    await session.flush()
    conv = await get_or_create_conversation(session, channel, contact)
    await session.commit()
    conv = await _conversation(session, org, conv.id)
    message = await _send(session, conv, body)
    return await idem.respond({"conversation_id": conv.id, "contact_id": contact.id, "message": message}, 201)


@router.post("/conversations/{conv_id}/assign", summary="Asignar a un asesor y/o grupo")
async def assign(conv_id: int, body: AssignIn, ctx: ApiContext = Depends(require("conversations:write")),
                 session: AsyncSession = Depends(get_session)):
    from app.service import commit_and_broadcast

    conv = await _conversation(session, ctx.org, conv_id)
    await _agent(session, ctx.org, body.agent_id)
    if body.group_id is not None:
        g = await session.get(Group, body.group_id)
        if not g or g.organization_id != ctx.org:
            raise ApiError(422, "Grupo inválido")
    await set_actor(session, "api")
    conv.status, conv.assigned_agent_id = "human", body.agent_id
    if body.group_id is not None:
        conv.group_id = body.group_id
    await commit_and_broadcast(session, conv)
    return conversation_json(conv)


@router.post("/conversations/{conv_id}/close", summary="Cerrar (y tipificar) una conversación")
async def close(conv_id: int, body: CloseIn | None = None, ctx: ApiContext = Depends(require("conversations:write")),
                session: AsyncSession = Depends(get_session)):
    from app.service import close as close_conv
    from app.service import typification_by_name

    conv = await _conversation(session, ctx.org, conv_id)
    typ = None
    if body and body.typification:
        typ = await typification_by_name(session, ctx.org, body.typification)
        if not typ or not typ.is_active:
            raise ApiError(422, "Tipificación inválida")
    await close_conv(session, conv, typ, actor="api")
    return conversation_json(conv)


# --- Negocios ------------------------------------------------------------------------
class DealIn(BaseModel):
    name: str
    contact_id: int | None = None
    phone: str | None = Field(default=None, description="Alternativa a contact_id: se crea el contacto si no existe")
    conversation_id: int | None = None
    owner_agent_id: int | None = None
    amount: float | None = None
    currency: str | None = None
    pipeline: str = "default"
    stage: str | None = None
    expected_close: date | None = None


class DealPatch(BaseModel):
    name: str | None = None
    owner_agent_id: int | None = None
    amount: float | None = None
    currency: str | None = None
    stage: str | None = Field(default=None, description="Mover a won/lost cierra el negocio")
    expected_close: date | None = None


class WonIn(BaseModel):
    amount: float | None = None


class LostIn(BaseModel):
    reason: str | None = None


async def _deal(session: AsyncSession, org: int, deal_id: int) -> Deal:
    d = await session.get(Deal, deal_id)
    if not d or d.organization_id != org:
        raise ApiError(404, "Negocio no encontrado")
    return d


def _http(e) -> ApiError:
    return ApiError(e.status_code, e.detail if isinstance(e.detail, str) else str(e.detail))


@router.get("/deals", summary="Listar negocios")
async def list_deals(status: str | None = Query(default=None, pattern="^(open|won|lost)$"), stage: str | None = None,
                     contact_id: int | None = None, updated_since: str | None = None, limit: int = 50,
                     cursor: str | None = None, ctx: ApiContext = Depends(require("deals:read")),
                     session: AsyncSession = Depends(get_session)):
    limit = _limit(limit)
    stmt = select(Deal).where(Deal.organization_id == ctx.org)
    for col, value in ((Deal.status, status), (Deal.stage, stage), (Deal.contact_id, contact_id)):
        if value is not None:
            stmt = stmt.where(col == value)
    if updated_since:
        stmt = stmt.where(Deal.updated_at >= updated_since)
    after = decode_cursor(cursor)
    if after:
        stmt = stmt.where(Deal.id < after)
    rows = (await session.scalars(stmt.order_by(Deal.id.desc()).limit(limit + 1))).unique().all()
    out = page(list(rows), limit, lambda d: d.id)
    out["data"] = [await deal_json(session, d) for d in out["data"]]
    return out


@router.get("/deals/{deal_id}", summary="Obtener un negocio")
async def get_deal(deal_id: int, ctx: ApiContext = Depends(require("deals:read")),
                   session: AsyncSession = Depends(get_session)):
    return await deal_json(session, await _deal(session, ctx.org, deal_id))


@router.post("/deals", summary="Crear un negocio", status_code=201)
async def create_deal(body: DealIn, idem: Idempotency = Depends(idempotency("deals:write")),
                      session: AsyncSession = Depends(get_session)):
    if idem.replay:
        return idem.replay
    from fastapi import HTTPException

    from app.crm import pipeline as pl
    from app.routers.contacts import normalize_phone
    from app.routers.deals import _apply_stage, _log_event, _validate_refs
    from app.service import get_or_create_contact

    org = idem.org
    contact_id = body.contact_id
    if contact_id is None:
        phone = normalize_phone(body.phone or "")
        if not phone:
            raise ApiError(422, "Indica `contact_id` o un `phone` válido")
        contact, _ = await get_or_create_contact(session, org, phone)
        await session.flush()
        contact_id = contact.id
    data = {**body.model_dump(), "contact_id": contact_id}
    try:
        await _validate_refs(session, org, data)
        config = await pl.get_pipelines(session, org)
        stages = pl.stage_keys(config, body.pipeline)
        if not stages:
            raise ApiError(422, "Embudo inválido")
        deal = Deal(organization_id=org, contact_id=contact_id, conversation_id=body.conversation_id,
                    owner_agent_id=body.owner_agent_id, name=body.name.strip(), amount=body.amount,
                    currency=(body.currency or config.get("currency") or "COP").upper()[:3], pipeline=body.pipeline,
                    stage=stages[0], expected_close=body.expected_close, source="api")
        session.add(deal)
        event = _apply_stage(deal, body.stage or stages[0], stages)
    except HTTPException as e:
        raise e if isinstance(e, ApiError) else _http(e) from None
    await session.flush()
    if event:
        await set_actor(session, "api")
        await _log_event(session, deal, event, None)
    await session.commit()
    return await idem.respond(await deal_json(session, deal), 201)


async def _close_deal(session: AsyncSession, deal: Deal, stage: str) -> dict:
    from fastapi import HTTPException

    from app.crm import pipeline as pl
    from app.routers.deals import _apply_stage, _log_event

    config = await pl.get_pipelines(session, deal.organization_id)
    try:
        event = _apply_stage(deal, stage, pl.stage_keys(config, deal.pipeline))
    except HTTPException as e:
        raise _http(e) from None
    if event:
        await set_actor(session, "api")
        await _log_event(session, deal, event, None)
    await session.commit()
    await session.refresh(deal)
    return await deal_json(session, deal)


@router.patch("/deals/{deal_id}", summary="Actualizar o mover de etapa un negocio")
async def patch_deal(deal_id: int, body: DealPatch, ctx: ApiContext = Depends(require("deals:write")),
                     session: AsyncSession = Depends(get_session)):
    from fastapi import HTTPException

    from app.routers.deals import _validate_refs

    deal = await _deal(session, ctx.org, deal_id)
    data = body.model_dump(exclude_unset=True)
    try:
        await _validate_refs(session, ctx.org, data)
    except HTTPException as e:
        raise _http(e) from None
    stage = data.pop("stage", None)
    for k, v in data.items():
        if k == "name" and v:
            v = v.strip()
        if k == "currency" and v:
            v = v.upper()[:3]
        setattr(deal, k, v)
    if stage:
        return await _close_deal(session, deal, stage)
    await session.commit()
    await session.refresh(deal)
    return await deal_json(session, deal)


@router.post("/deals/{deal_id}/won", summary="Marcar como ganado")
async def won(deal_id: int, body: WonIn | None = None, ctx: ApiContext = Depends(require("deals:write")),
              session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, ctx.org, deal_id)
    if body and body.amount is not None:
        if body.amount < 0:
            raise ApiError(422, "El monto no puede ser negativo")
        deal.amount = body.amount
    return await _close_deal(session, deal, "won")


@router.post("/deals/{deal_id}/lost", summary="Marcar como perdido")
async def lost(deal_id: int, body: LostIn | None = None, ctx: ApiContext = Depends(require("deals:write")),
               session: AsyncSession = Depends(get_session)):
    deal = await _deal(session, ctx.org, deal_id)
    if body and body.reason:
        deal.lost_reason = body.reason
    return await _close_deal(session, deal, "lost")


# --- Reportes ------------------------------------------------------------------------
@router.get("/reports/summary", summary="Resumen del periodo (por defecto, últimos 7 días)")
async def report_summary(start: date | None = None, end: date | None = None,
                         ctx: ApiContext = Depends(require("reports:read")), session: AsyncSession = Depends(get_session)):
    from app.routers.reports import _range

    lo, hi, _tz, start, end = await _range(session, ctx.org, start, end)
    c = (await session.execute(text("""
        select coalesce(sum(new_conversations), 0), coalesce(sum(closed), 0), coalesce(sum(sales), 0),
               coalesce(sum(handoffs), 0), coalesce(sum(inbound_messages), 0), coalesce(sum(bot_messages), 0),
               coalesce(sum(agent_messages), 0), coalesce(sum(first_responses), 0),
               coalesce(sum(first_response_sum_s), 0), coalesce(sum(first_response_within_sla), 0)
        from reporting.daily_conversations where organization_id = :o and day between :a and :b"""),
        {"o": ctx.org, "a": start, "b": end})).one()
    d = (await session.execute(text("""
        select count(*) filter (where status = 'won'), coalesce(sum(amount) filter (where status = 'won'), 0),
               count(*) filter (where status = 'lost')
        from public.deals where organization_id = :o and closed_at >= :lo and closed_at < :hi"""),
        {"o": ctx.org, "lo": lo, "hi": hi})).one()
    return {"start": start, "end": end,
            "conversations": {"new": c[0], "closed": c[1], "sales": c[2], "handoffs": c[3]},
            "messages": {"inbound": c[4], "bot": c[5], "agent": c[6]},
            "first_response": {"count": c[7], "avg_seconds": round(c[8] / c[7]) if c[7] else None,
                               "within_sla_pct": round(100 * c[9] / c[7], 1) if c[7] else None},
            "deals": {"won": d[0], "won_amount": float(d[1]), "lost": d[2]}}


# --- Webhooks (REST Hooks para Zapier, Make y n8n) --------------------------------------
class HookIn(BaseModel):
    url: str = Field(description="https://…")
    events: list[str] = Field(default=[], description="Vacío = todos")
    name: str | None = None


def _hook_json(w: OutboundWebhook, secret: str | None = None) -> dict:
    out = {"id": w.id, "name": w.name, "url": w.url, "events": list(w.events or []), "source": w.source,
           "active": w.active, "created_at": w.created_at}
    if secret is not None:
        out["secret"] = secret
    return out


@router.get("/events", summary="Eventos disponibles para webhooks")
async def events(_: ApiContext = Depends(api_context)):
    from app.realtime import PUBLIC_EVENTS

    return [{"event": e, "description": EVENT_DESCRIPTIONS.get(e, "")} for e in sorted(PUBLIC_EVENTS)]


@router.get("/webhooks", summary="Suscripciones creadas por la API")
async def list_hooks(ctx: ApiContext = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(OutboundWebhook).where(
        OutboundWebhook.organization_id == ctx.org, OutboundWebhook.source != "panel")
        .order_by(OutboundWebhook.id))).all()
    return {"data": [_hook_json(w) for w in rows], "next_cursor": None}


@router.post("/webhooks", summary="Suscribir un webhook (REST Hooks)", status_code=201)
async def subscribe(body: HookIn, x_connector: str | None = Header(default=None),
                    idem: Idempotency = Depends(idempotency("webhooks:manage")),
                    session: AsyncSession = Depends(get_session)):
    """Devuelve el `secret` con el que se firma cada entrega (cabecera X-Signature-256). Solo se muestra aquí."""
    if idem.replay:
        return idem.replay
    from app.realtime import PUBLIC_EVENTS
    from app.secrets_vault import put_secret
    from app.webhooks_out import invalidate_cache

    source = (x_connector or "api").strip().lower()
    if source not in CONNECTORS:
        raise ApiError(422, f"X-Connector inválido: usa {', '.join(CONNECTORS)}")
    if not body.url.startswith("https://"):
        raise ApiError(422, "La URL debe usar https://")
    bad = set(body.events) - PUBLIC_EVENTS
    if bad:
        raise ApiError(422, f"Eventos inválidos: {', '.join(sorted(bad))}")
    w = OutboundWebhook(organization_id=idem.org, name=(body.name or f"{source}: {', '.join(body.events) or 'todos'}")[:120],
                        url=body.url, events=sorted(set(body.events)), active=True, source=source,
                        api_key_id=idem.ctx.key.id)
    session.add(w)
    await session.flush()
    secret = secrets.token_hex(24)
    w.signing_secret_id = await put_secret(session, secret, f"outbound_webhook:{w.id}")
    await session.commit()
    invalidate_cache()
    return await idem.respond(_hook_json(w, secret), 201)


@router.delete("/webhooks/{hook_id}", summary="Cancelar una suscripción")
async def unsubscribe(hook_id: int, ctx: ApiContext = Depends(require("webhooks:manage")),
                      session: AsyncSession = Depends(get_session)):
    from app.secrets_vault import delete_secret
    from app.webhooks_out import invalidate_cache

    w = await session.get(OutboundWebhook, hook_id)
    if not w or w.organization_id != ctx.org or w.source == "panel":
        raise ApiError(404, "Suscripción no encontrada")
    await delete_secret(session, w.signing_secret_id)
    await session.delete(w)
    await session.commit()
    invalidate_cache()
    return {"deleted": True, "id": hook_id}


@router.get("/webhooks/sample/{event}", summary="Ejemplo del cuerpo de un evento (lista, para Zapier)")
async def sample(event: str, _: ApiContext = Depends(api_context)):
    if event not in SAMPLES:
        raise ApiError(404, "Evento desconocido")
    return [SAMPLES[event]]

