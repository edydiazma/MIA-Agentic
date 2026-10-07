from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage, templates
from app.auth import current_agent
from app.campaigns import send_template_message
from app.db import get_session, set_actor
from app.identity import wa_address
from app.models import (
    Agent,
    Channel,
    Contact,
    ContactIdentity,
    Conversation,
    ConversationTag,
    Group,
    Message,
    Organization,
    Resource,
    Tag,
    utcnow,
)
from app.schemas import SendText
from app.service import (
    commit_and_broadcast,
    conversation_out,
    get_conversation,
    get_message,
    message_out,
    record_message,
    send_text,
    system_note,
    typification_by_name,
    wa_client,
    within_session_window,
)
from app.service import close as close_conv
from app.scope import conversation_clause, require_conversation, scope_for
from app.settings_store import get_setting

router = APIRouter(prefix="/api", tags=["inbox"])

MAX_UPLOAD = 16 * 1024 * 1024  # límite de WhatsApp para la mayoría de medios


class TransferIn(BaseModel):
    agent_id: int | None = None
    group_id: int | None = None
    note: str | None = None


class CloseIn(BaseModel):
    typification: str | None = None
    # Datos que exige la tipificación (deal.amount, deal.currency, field:<clave>…), §17
    values: dict[str, str | float | int | None] | None = None


class TemplateIn(BaseModel):
    name: str
    language: str
    values: list[str] = []


class ResourceSend(BaseModel):
    resource_id: int
    caption: str | None = None


async def _conversation(session: AsyncSession, conv_id: int, agent: Agent) -> Conversation:
    """La conversación, si existe y está en el alcance del usuario (app/scope.py); si no, 404."""
    return await require_conversation(session, agent, await get_conversation(session, conv_id, agent.organization_id))


def _require_window(conv: Conversation) -> None:
    if within_session_window(conv, human=True):
        return
    if conv.channel.provider == "whatsapp_cloud":
        raise HTTPException(
            409, "Pasaron más de 24 h desde el último mensaje del cliente: WhatsApp solo permite plantillas aprobadas.")
    raise HTTPException(409, "Pasaron más de 7 días desde el último mensaje del cliente: Messenger e Instagram no "
                             "permiten escribirle hasta que vuelva a escribir.")


async def _take(session: AsyncSession, conv: Conversation, agent: Agent) -> None:
    """Enviar como asesor implica tomar la conversación (el bot deja de responder)."""
    if conv.status != "human" or conv.assigned_agent_id != agent.id:
        await set_actor(session, "agent", agent.id)
        first = conv.assigned_agent_id is None
        conv.status, conv.assigned_agent_id = "human", agent.id
        if first:  # dueño del cliente en la primera asignación (app.routing)
            from app.routing import after_assignment

            await after_assignment(session, conv, agent.id, "take")
        await commit_and_broadcast(session, conv)


SUBSTATES = "^(new|returning|reassigned|active)$"


def _substate_clause(substate: str):
    """Sub-estados de la bandeja (§18.1): nueva, recurrente, reasignada, activa."""
    if substate == "new":
        return (Conversation.assigned_agent_id.is_(None)) & (Conversation.is_returning.is_(False)) \
            & (Conversation.status != "closed")
    if substate == "returning":
        return (Conversation.is_returning.is_(True)) & (Conversation.status != "closed")
    if substate == "reassigned":
        return (Conversation.assignment_count > 1) & (Conversation.status != "closed")
    return (Conversation.assigned_agent_id.is_not(None)) & (Conversation.status != "closed")


@router.get("/conversations")
async def list_conversations(
    status: str | None = Query(default=None, pattern="^(bot|human|closed|open|unassigned)$"),
    mine: bool = False,
    agent_id: int | None = None,  # "actuar como agente" (solo admin/supervisor)
    group_id: int | None = None,
    tag: str | None = None,
    q: str | None = None,
    channel: str | None = Query(default=None, pattern="^(whatsapp_cloud|messenger|instagram|webchat|email)$"),
    substate: str | None = Query(default=None, pattern=SUBSTATES),
    date_from: date | None = None,  # fecha de creación (zona de la empresa)
    date_to: date | None = None,
    typification_id: int | None = None,
    conversation_id: int | None = None,
    agent: str | None = Query(default=None, alias="agent", max_length=100),  # nombre del asesor
    group: str | None = Query(default=None, max_length=100),  # nombre del grupo
    owner_agent_id: int | None = None,
    limit: int = Query(default=100, le=500),
    me: Agent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    org = me.organization_id
    scope = await scope_for(session, me)
    stmt = select(Conversation).where(Conversation.organization_id == org)
    clause = conversation_clause(scope)
    if clause is not None:
        stmt = stmt.where(clause)
    if status == "open":
        stmt = stmt.where(Conversation.status != "closed")
    elif status == "unassigned":
        stmt = stmt.where(Conversation.status == "human", Conversation.assigned_agent_id.is_(None))
    elif status:
        stmt = stmt.where(Conversation.status == status)
    if agent_id is not None:
        if me.role not in ("admin", "supervisor") and agent_id != me.id:
            raise HTTPException(403, "Solo un administrador puede actuar como otro agente")
        stmt = stmt.where(Conversation.assigned_agent_id == agent_id)
    elif mine:
        stmt = stmt.where(Conversation.assigned_agent_id == me.id)
    if group_id:
        stmt = stmt.where(Conversation.group_id == group_id)
    if tag:
        stmt = stmt.where(exists().where(
            ConversationTag.conversation_id == Conversation.id, ConversationTag.tag_id == Tag.id,
            Tag.organization_id == org, Tag.name == tag.strip().lower()))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            exists().where(Contact.id == Conversation.contact_id,
                           or_(Contact.name.ilike(like), Contact.wa_id.ilike(like), Contact.email.ilike(like))),
            exists().where(ContactIdentity.contact_id == Conversation.contact_id,
                           or_(ContactIdentity.username.ilike(like), ContactIdentity.external_id.ilike(like)))))
    if channel:
        stmt = stmt.where(exists().where(Channel.id == Conversation.channel_id, Channel.provider == channel))
    if substate:
        stmt = stmt.where(_substate_clause(substate))
    if date_from or date_to:
        tz = ZoneInfo((await session.get(Organization, org)).timezone)
        if date_from:
            stmt = stmt.where(Conversation.created_at >= datetime.combine(date_from, time.min, tz))
        if date_to:
            stmt = stmt.where(Conversation.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tz))
    if typification_id:
        stmt = stmt.where(Conversation.typification_id == typification_id)
    if conversation_id:
        stmt = stmt.where(Conversation.id == conversation_id)
    if agent:
        stmt = stmt.where(exists().where(Agent.id == Conversation.assigned_agent_id,
                                         Agent.name.ilike(f"%{agent.strip()}%")))
    if group:
        stmt = stmt.where(exists().where(Group.id == Conversation.group_id, Group.name.ilike(f"%{group.strip()}%")))
    if owner_agent_id:
        stmt = stmt.where(exists().where(Contact.id == Conversation.contact_id,
                                         Contact.owner_agent_id == owner_agent_id))
    stmt = stmt.order_by(Conversation.last_message_at.desc()).limit(limit)
    return [conversation_out(c) for c in (await session.scalars(stmt)).unique().all()]


@router.get("/conversations/counts")
async def conversation_counts(me: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Contadores de la bandeja dentro del alcance del usuario (pestañas por sub-estado)."""
    org = me.organization_id
    scope = await scope_for(session, me)
    base = [Conversation.organization_id == org]
    clause = conversation_clause(scope)
    if clause is not None:
        base.append(clause)
    tz = ZoneInfo((await session.get(Organization, org)).timezone)
    today = datetime.combine(utcnow().astimezone(tz).date(), time.min, tz)
    open_ = Conversation.status != "closed"

    def n(*conds):
        return func.count().filter(*conds)

    row = (await session.execute(select(
        n(_substate_clause("new")), n(_substate_clause("returning")), n(_substate_clause("reassigned")),
        n(_substate_clause("active")),
        n(Conversation.status == "human", Conversation.assigned_agent_id.is_(None)),
        n(open_, Conversation.assigned_agent_id == me.id),
        n(Conversation.closed_at >= today)).where(*base))).one()
    keys = ("new", "returning", "reassigned", "active", "unassigned", "mine", "closed_today")
    return dict(zip(keys, (int(v or 0) for v in row), strict=True))


@router.get("/conversations/{conv_id}")
async def get_conversation_detail(conv_id: int, agent: Agent = Depends(current_agent),
                                  session: AsyncSession = Depends(get_session)):
    return conversation_out(await _conversation(session, conv_id, agent))


@router.get("/conversations/{conv_id}/messages")
async def list_messages(
    conv_id: int, before: int | None = None, limit: int = Query(default=50, le=200),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    await _conversation(session, conv_id, agent)
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(
        Message.created_at.desc(), Message.id.desc()).limit(limit)
    if before:
        stmt = stmt.where(Message.id < before)
    rows = (await session.scalars(stmt)).all()
    return [message_out(m) for m in reversed(rows)]


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: int, body: SendText, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    if not body.text.strip():
        raise HTTPException(422, "Mensaje vacío")
    conv = await _conversation(session, conv_id, agent)
    _require_window(conv)
    await _take(session, conv, agent)
    sent = await send_text(session, conv, body.text.strip(), sender_type="agent", agent_id=agent.id)
    return message_out(sent[0])


async def _send_file(session: AsyncSession, conv: Conversation, agent: Agent, data: bytes, mime: str,
                     filename: str, caption: str | None) -> Message:
    kind = storage.kind_for_mime(mime)
    path = await storage.upload(
        storage.new_path(storage.MEDIA_BUCKET, conv.organization_id, f"conv/{conv.id}", mime), data, mime)
    msg = Message(direction="out", sender_type="agent", sender_agent_id=agent.id, type=kind, text=caption,
                  media_path=path, media_mime=mime, media_filename=filename, media_size=len(data))
    try:
        client = await wa_client(session, conv.channel, conv)
        client.human = True
        media_id = await client.upload_media(data, mime, filename)
        msg.wa_message_id = await client.send_media(wa_address(conv.contact), kind, media_id, caption, filename)
        msg.status = "sent"
    except Exception as e:
        msg.status, msg.error = "failed", str(e)[:2000]
    return await record_message(session, conv, msg)


@router.post("/conversations/{conv_id}/attachments")
async def send_attachment(
    conv_id: int, file: UploadFile = File(...), caption: str | None = Form(default=None),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    conv = await _conversation(session, conv_id, agent)
    _require_window(conv)
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Archivo demasiado grande (máx. 16 MB)")
    await _take(session, conv, agent)
    msg = await _send_file(session, conv, agent, data, file.content_type or "application/octet-stream",
                           file.filename or "archivo", caption)
    return message_out(msg)


@router.post("/conversations/{conv_id}/resource")
async def send_resource(conv_id: int, body: ResourceSend, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, conv_id, agent)
    _require_window(conv)
    res = await session.get(Resource, body.resource_id)
    if not res or res.organization_id != agent.organization_id:
        raise HTTPException(404, "Recurso no encontrado")
    await _take(session, conv, agent)
    msg = await _send_file(session, conv, agent, await storage.download(res.storage_path), res.mime, res.name,
                           body.caption)
    return message_out(msg)


@router.post("/conversations/{conv_id}/template")
async def send_template(conv_id: int, body: TemplateIn, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    """Plantilla individual: permite escribir fuera de la ventana de 24 h."""
    conv = await _conversation(session, conv_id, agent)
    if conv.channel.provider != "whatsapp_cloud":
        raise HTTPException(409, "Las plantillas son solo de WhatsApp")
    catalog = await templates.list_templates(session, conv.channel)
    tpl = next((t for t in catalog if t["name"] == body.name and t["language"] == body.language), None)
    if not tpl or tpl["status"] != "APPROVED":
        raise HTTPException(404, "Plantilla no encontrada o no aprobada")
    if not tpl["supported"]:
        raise HTTPException(422, tpl["unsupported_reason"])
    if tpl["category"] == "MARKETING" and conv.contact.marketing_opt_out:
        raise HTTPException(409, "El contacto pidió no recibir marketing: usa una plantilla de utilidad")
    try:
        templates.build(tpl, body.values)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    await _take(session, conv, agent)
    msg = await send_template_message(session, conv, tpl, body.values, sender_type="agent", agent_id=agent.id)
    return message_out(msg)


@router.post("/conversations/{conv_id}/assign")
async def assign_to_me(conv_id: int, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, conv_id, agent)
    await _take(session, conv, agent)
    return conversation_out(conv)


@router.post("/conversations/{conv_id}/transfer")
async def transfer(conv_id: int, body: TransferIn, agent: Agent = Depends(current_agent),
                   session: AsyncSession = Depends(get_session)):
    """Transfiere a otro asesor y/o grupo."""
    conv = await _conversation(session, conv_id, agent)
    org = agent.organization_id
    if body.agent_id:
        target = await session.get(Agent, body.agent_id)
        if not target or target.organization_id != org:
            raise HTTPException(404, "Asesor no encontrado")
    if body.group_id:
        g = await session.get(Group, body.group_id)
        if not g or g.organization_id != org:
            raise HTTPException(404, "Grupo no encontrado")
    from app import routing

    source = await session.get(Group, conv.group_id) if conv.group_id else None
    routing.check_transfer(conv, source, body.group_id, agent.role)  # grupos destino permitidos (§18.1)
    await set_actor(session, "agent", agent.id)
    target_agent = body.agent_id
    if body.group_id is not None:
        conv.group_id = body.group_id or None
    if not target_agent and body.group_id and (await get_setting(session, "conversations", org))["auto_assign"]:
        target_agent = await routing.pick_agent(session, org, conv.group_id, conv.contact_id,
                                                exclude={conv.assigned_agent_id} if conv.assigned_agent_id else None)
    conv.status, conv.assigned_agent_id = "human", target_agent
    await routing.after_assignment(session, conv, target_agent, "transfer")
    await system_note(session, conv, f"Transferida por {agent.name}" + (f": {body.note}" if body.note else ""))
    await commit_and_broadcast(session, conv)
    return conversation_out(conv)


@router.post("/conversations/{conv_id}/release")
async def release_to_bot(conv_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, conv_id, agent)
    await set_actor(session, "agent", agent.id)
    conv.status, conv.assigned_agent_id, conv.handoff_reason, conv.handoff_at = "bot", None, None, None
    await commit_and_broadcast(session, conv)
    return conversation_out(conv)


@router.post("/conversations/{conv_id}/close")
async def close_conversation(conv_id: int, body: CloseIn | None = None, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, conv_id, agent)
    cfg = await get_setting(session, "conversations", agent.organization_id)
    name = (body.typification if body else None) or None
    typ = await typification_by_name(session, agent.organization_id, name) if name else conv.typification
    if name and (not typ or not typ.is_active):
        raise HTTPException(422, "Tipificación inválida")
    if cfg["require_typification"] and not typ:
        raise HTTPException(422, "Selecciona una tipificación para cerrar la conversación")
    if typ is not None and typ.required_fields:
        from app.agent_config import missing_required

        missing = await missing_required(session, conv, typ, body.values if body else None)
        if missing:
            raise HTTPException(422, {"message": f"La tipificación «{typ.name}» exige: {', '.join(missing)}",
                                      "missing": missing})
    await close_conv(session, conv, typ, actor="agent", agent_id=agent.id)
    return conversation_out(conv)


@router.post("/conversations/{conv_id}/read")
async def mark_read(conv_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conv = await _conversation(session, conv_id, agent)
    if conv.unread_count:
        conv.unread_count = 0
        await commit_and_broadcast(session, conv)
    return {"ok": True}


@router.get("/media/{message_id}")
async def get_media(message_id: int, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    msg = await get_message(session, message_id)
    if not msg or msg.organization_id != agent.organization_id or not msg.media_path:
        raise HTTPException(404, "Sin archivo")
    data = await storage.download(msg.media_path)
    headers = {"Content-Disposition": f'inline; filename="{msg.media_filename or "archivo"}"'}
    return Response(content=data, media_type=msg.media_mime or "application/octet-stream", headers=headers)
