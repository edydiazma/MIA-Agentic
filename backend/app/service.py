"""Operaciones de dominio compartidas (webhook, agente IA, automatizaciones, bandeja, flujos).

La base mantiene por triggers: contadores y último mensaje de la conversación, primera respuesta,
eventos (conversation_events), cierre/reapertura y expiración de sugerencias. Este módulo NO duplica esa lógica:
escribe el cambio, indica el actor con db.set_actor() y relee la conversación.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_actor
from app.fields import custom_values
from app.models import (
    Agent,
    AgentGroup,
    Alert,
    Channel,
    Contact,
    Conversation,
    ConversationTag,
    Message,
    Tag,
    Typification,
)
from app.realtime import hub
from app.schemas import AgentOut, ContactOut, ConversationOut, GroupOut, MessageOut
from app.settings_store import get_setting
from app.channels import LABELS, channel_client, window_for
from app.identity import wa_address
from app.models import ContactIdentity

SESSION_WINDOW = timedelta(hours=24)


# --- Serialización (contrato estable de la API) ---------------------------------
def _days(delta) -> float | None:
    return round(delta.total_seconds() / 86400, 2) if delta is not None else None


def contact_out(c: Contact) -> dict:
    first, last = as_utc(c.first_interaction_at), as_utc(c.last_interaction_at)
    return ContactOut(
        id=c.id, wa_id=c.wa_id, avatar_url=c.avatar_url, name=c.name, email=c.email, notes=c.notes, stage=c.stage,
        tags=sorted(link.tag.name for link in c.tag_links), custom_fields=custom_values(c), memory=c.memory,
        memory_updated_at=c.memory_updated_at, blocked=c.blocked, blocked_reason=c.blocked_reason,
        blocked_at=c.blocked_at, marketing_opt_out=c.marketing_opt_out, created_at=c.created_at,
        wa_username=c.wa_username, wa_bsuid=c.wa_bsuid, channel_providers=list(c.channel_providers or []),
        updated_at=c.updated_at, first_interaction_at=first, first_inbound_at=c.first_inbound_at,
        last_interaction_at=last, last_inbound_at=c.last_inbound_at, last_outbound_at=c.last_outbound_at,
        messages_in=c.messages_in or 0, messages_out=c.messages_out or 0,
        conversations_count=c.conversations_count or 0, flow_runs_count=c.flow_runs_count or 0,
        last_flow_at=c.last_flow_at, products_count=c.products_count or 0, last_product_name=c.last_product_name,
        lifetime_days=_days(last - first) if first and last else None,
        days_since_last_interaction=_days(datetime.now(UTC) - last) if last else None,
    ).model_dump(mode="json")


def suggestions_out(conv: Conversation) -> dict | None:
    out: dict = {}
    for s in conv.pending_suggestions:
        if s.kind == "tag":
            out.setdefault("tags", []).append(s.value)
        elif s.kind == "typification":
            out["typification"] = {"value": s.value, "confidence": s.confidence}
        elif s.kind == "group":
            out["group"] = {"value": s.value.get("name"), "group_id": s.value.get("group_id"),
                            "confidence": s.confidence}
        elif s.kind == "field":
            out.setdefault("fields", []).append({"key": s.target, "label": s.value.get("label"),
                                                 "value": s.value.get("value"), "confidence": s.confidence,
                                                 "evidence": s.evidence or ""})
    return out or None


def conversation_out(c: Conversation) -> dict:
    return ConversationOut(
        id=c.id, status=c.status, contact=contact_out(c.contact), channel_id=c.channel_id, ai_agent_id=c.ai_agent_id,
        channel_provider=c.channel.provider, channel_name=c.channel.name,
        channel_label=LABELS.get(c.channel.provider, c.channel.provider), window_open=within_session_window(c),
        assigned_agent=AgentOut.model_validate(c.assigned_agent) if c.assigned_agent else None,
        group=GroupOut.model_validate(c.group) if c.group else None,
        handoff_reason=c.handoff_reason, handoff_at=c.handoff_at, first_response_at=c.first_response_at,
        typification=c.typification.name if c.typification else None,
        tags=sorted(link.tag.name for link in c.tag_links),
        ai_summary=c.ai_summary, ai_sentiment=c.ai_sentiment,
        ai_typification=c.ai_typification.name if c.ai_typification else None,
        ai_suggestions=suggestions_out(c), ai_classified_at=c.ai_classified_at, closed_at=c.closed_at,
        ad_source_type=c.ad_source_type, ad_headline=c.ad_headline, unread_count=c.unread_count,
        message_count=c.message_count, last_message_at=c.last_message_at, last_inbound_at=c.last_inbound_at,
        last_message_preview=c.last_message_preview, created_at=c.created_at,
    ).model_dump(mode="json")


def message_out(m: Message) -> dict:
    return MessageOut(
        id=m.id, conversation_id=m.conversation_id, direction=m.direction, sender_type=m.sender_type,
        sender_agent_id=m.sender_agent_id, type=m.type, text=m.text, media_mime=m.media_mime,
        media_filename=m.media_filename, transcript=m.transcript, template_name=m.template_name,
        has_media=bool(m.media_path), status=m.status, error=m.error, created_at=m.created_at,
    ).model_dump(mode="json")


# --- Utilidades -----------------------------------------------------------------
def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def within_session_window(conv: Conversation, human: bool = True) -> bool:
    """Ventana de mensajes libres desde el último mensaje del cliente: WhatsApp 24 h; Messenger e Instagram
    24 h (7 días para respuestas de asesores, etiqueta HUMAN_AGENT); chat web sin límite."""
    window = window_for(conv.channel.provider, human)
    if window is None:
        return True
    last = as_utc(conv.last_inbound_at)
    return bool(last and datetime.now(UTC) - last < window)


async def wa_client(session: AsyncSession, channel: Channel, conv: Conversation | None = None):
    """Cliente del canal (WhatsApp, Messenger, Instagram o chat web); los tokens viven en Vault.
    El nombre es histórico: devuelve el cliente que corresponda al proveedor del canal (app/channels)."""
    return await channel_client(session, channel, conv)


async def get_message(session: AsyncSession, message_id: int) -> Message | None:
    """Tabla particionada (PK compuesta): se busca por id."""
    return await session.scalar(select(Message).where(Message.id == message_id))


async def get_conversation(session: AsyncSession, conv_id: int, org: int) -> Conversation | None:
    conv = await session.get(Conversation, conv_id)
    return conv if conv and conv.organization_id == org else None


async def reload(session: AsyncSession, conv: Conversation) -> Conversation:
    """Relee la conversación y sus relaciones (los triggers cambian columnas en la base)."""
    await session.refresh(conv)
    for rel in ("contact", "channel", "tag_links", "pending_suggestions", "group", "assigned_agent", "typification",
                "ai_typification"):
        await session.refresh(conv, [rel])
    await session.refresh(conv.contact, ["field_values", "tag_links"])
    return conv


async def broadcast_conversation(conv: Conversation, event: str = "conversation.updated") -> None:
    data = conversation_out(conv)
    await hub.broadcast("conversation.updated", data, conv.organization_id)
    if event != "conversation.updated":
        await hub.broadcast(event, data, conv.organization_id)


async def commit_and_broadcast(session: AsyncSession, conv: Conversation, event: str = "conversation.updated") -> None:
    await session.commit()
    await reload(session, conv)
    await broadcast_conversation(conv, event)


# --- Contactos y conversaciones -------------------------------------------------
async def get_or_create_contact(session: AsyncSession, org: int, wa_id: str | None,
                                profile_name: str | None = None, bsuid: str | None = None,
                                username: str | None = None, parent_bsuid: str | None = None) -> tuple[Contact, bool]:
    """Contacto de WhatsApp por BSUID (user_id) y/o teléfono; ver app/identity.py."""
    from app.identity import resolve_whatsapp_contact

    return await resolve_whatsapp_contact(session, org, phone=wa_id, bsuid=bsuid, parent_bsuid=parent_bsuid,
                                          username=username, name=profile_name)


async def get_or_create_contact_by_identity(session: AsyncSession, channel: Channel, external_id: str,
                                            name: str | None = None, username: str | None = None,
                                            avatar_url: str | None = None,
                                            profile: dict | None = None) -> tuple[Contact, ContactIdentity, bool]:
    """Messenger, Instagram y chat web: el contacto se identifica por su id en el canal (PSID, IGSID, visitante)."""
    ident = await session.scalar(select(ContactIdentity).where(
        ContactIdentity.channel_id == channel.id, ContactIdentity.external_id == external_id))
    if ident:
        contact = await session.get(Contact, ident.contact_id)
        if name and not contact.name:
            contact.name = name
        if avatar_url and not contact.avatar_url:
            contact.avatar_url = avatar_url
        if username and not ident.username:
            ident.username = username
        return contact, ident, False
    contact = Contact(organization_id=channel.organization_id, name=name or (f"@{username}" if username else None),
                      avatar_url=avatar_url)
    session.add(contact)
    await session.flush()
    await session.refresh(contact, ["field_values", "tag_links"])
    ident = ContactIdentity(organization_id=channel.organization_id, contact_id=contact.id, provider=channel.provider,
                            channel_id=channel.id, external_id=external_id, username=username, profile=profile or {})
    session.add(ident)
    await session.flush()
    return contact, ident, True


async def get_or_create_conversation(session: AsyncSession, channel: Channel, contact: Contact,
                                     reopen: bool = True) -> Conversation:
    conv = await session.scalar(
        select(Conversation).where(Conversation.contact_id == contact.id, Conversation.channel_id == channel.id)
        .order_by(Conversation.id.desc()).limit(1))
    if not conv:
        conv = Conversation(organization_id=channel.organization_id, contact_id=contact.id, channel_id=channel.id,
                            ai_agent_id=channel.default_ai_agent_id, status="bot")
        session.add(conv)
        await session.flush()
        await reload(session, conv)
    elif conv.status == "closed" and reopen:
        # El trigger de estado limpia closed_at, tipificación, handoff y cuenta la reapertura.
        conv.status, conv.assigned_agent_id = "bot", None
        conv.ai_agent_id = conv.ai_agent_id or channel.default_ai_agent_id
        await session.flush()
        await reload(session, conv)
    return conv


async def record_message(session: AsyncSession, conv: Conversation, msg: Message) -> Message:
    """Inserta el mensaje; los contadores, la vista previa y la primera respuesta los actualiza el trigger."""
    msg.organization_id = conv.organization_id
    msg.conversation_id = conv.id
    session.add(msg)
    await session.commit()
    await reload(session, conv)
    await hub.broadcast("message.new", message_out(msg), conv.organization_id)
    await broadcast_conversation(conv)
    if conv.channel.provider == "webchat" and msg.direction == "out":
        from app.channels.webchat import notify

        notify(conv.id)
    return msg


async def send_text(session: AsyncSession, conv: Conversation, text: str, sender_type: str,
                    agent_id: int | None = None, ai_agent_id: int | None = None) -> list[Message]:
    """Envía texto por el canal de la conversación (partiéndolo si es largo) y lo registra."""
    msg = Message(direction="out", sender_type=sender_type, sender_agent_id=agent_id, ai_agent_id=ai_agent_id,
                  type="text", text=text)
    try:
        client = await wa_client(session, conv.channel, conv)
        client.human = sender_type == "agent"  # Messenger/Instagram: HUMAN_AGENT fuera de las 24 h
        ids = await client.send_text(wa_address(conv.contact), text)
        msg.wa_message_id, msg.status = ids[0], "sent"
    except Exception as e:
        msg.status, msg.error = "failed", str(e)[:2000]
    return [await record_message(session, conv, msg)]


async def system_note(session: AsyncSession, conv: Conversation, text: str) -> None:
    """Nota interna visible en el chat (no se envía al cliente)."""
    session.add(Message(organization_id=conv.organization_id, conversation_id=conv.id, direction="out",
                        sender_type="system", type="text", text=text, status="sent"))


# --- Etiquetas y tipificaciones -------------------------------------------------
async def tag_by_name(session: AsyncSession, org: int, name: str, create: bool = True) -> Tag | None:
    name = name.strip().lower()
    if not name:
        return None
    tag = await session.scalar(select(Tag).where(Tag.organization_id == org, Tag.name == name))
    if not tag and create:
        tag = Tag(organization_id=org, name=name)
        session.add(tag)
        await session.flush()
    return tag


async def set_conversation_tags(session: AsyncSession, conv: Conversation, names: list[str], source: str,
                                agent_id: int | None = None, confidence: dict[str, float] | None = None,
                                replace: bool = True) -> list[str]:
    """Agrega (y si replace, quita) etiquetas. Devuelve las agregadas."""
    current = {link.tag.name: link for link in conv.tag_links}
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    added = []
    for name in wanted - current.keys():
        tag = await tag_by_name(session, conv.organization_id, name)
        session.add(ConversationTag(conversation_id=conv.id, tag_id=tag.id, source=source, created_by=agent_id,
                                    confidence=(confidence or {}).get(name)))
        added.append(name)
    if replace:
        for name in current.keys() - wanted:
            await session.delete(current[name])
    return added


async def typification_by_name(session: AsyncSession, org: int, name: str | None) -> Typification | None:
    if not name:
        return None
    return await session.scalar(select(Typification).where(
        Typification.organization_id == org, Typification.name == name))


# --- Asignación y transferencia -------------------------------------------------
async def pick_agent(session: AsyncSession, org: int, group_id: int | None) -> int | None:
    """Asesor conectado y disponible con menos conversaciones abiertas (del grupo, si hay)."""
    online = hub.online_agent_ids()
    if not online:
        return None
    stmt = select(Agent.id).where(Agent.organization_id == org, Agent.is_active, Agent.availability == "available",
                                  Agent.id.in_(online))
    if group_id:
        stmt = stmt.join(AgentGroup, AgentGroup.agent_id == Agent.id).where(AgentGroup.group_id == group_id)
    candidates = list((await session.scalars(stmt)).all())
    if not candidates:
        return None
    load = dict((await session.execute(
        select(Conversation.assigned_agent_id, func.count())
        .where(Conversation.status == "human", Conversation.assigned_agent_id.in_(candidates))
        .group_by(Conversation.assigned_agent_id))).all())
    return min(candidates, key=lambda a: (load.get(a, 0), a))


async def handoff(session: AsyncSession, conv: Conversation, reason: str, group_id: int | None = None,
                  actor: str = "bot") -> None:
    """Pasa de bot a humano, enruta con IA si hace falta, asigna y avisa."""
    await set_actor(session, actor)
    conv.status, conv.handoff_reason = "human", reason
    conv.handoff_at = datetime.now(UTC)
    if group_id:
        conv.group_id = group_id
    await session.commit()
    if not group_id:
        from app.classifier import route_on_handoff  # import diferido: classifier usa este módulo

        await route_on_handoff(session, conv)
    if (await get_setting(session, "conversations", conv.organization_id))["auto_assign"] and not conv.assigned_agent_id:
        await set_actor(session, "system")
        conv.assigned_agent_id = await pick_agent(session, conv.organization_id, conv.group_id)
    await commit_and_broadcast(session, conv, "conversation.handoff")

    from app.automations import after_handoff  # evita import circular

    await after_handoff(session, conv)
    if actor != "flow":  # un flujo que transfiere no vuelve a disparar flujos de transferencia
        from app.flows.engine import on_event

        await on_event(conv.id, "handoff")


async def close(session: AsyncSession, conv: Conversation, typification: Typification | None,
                actor: str = "agent", agent_id: int | None = None) -> None:
    """Cierra; el trigger fija closed_at, limpia no leídos y expira sugerencias."""
    await set_actor(session, actor, agent_id)
    conv.status = "closed"
    conv.typification_id = typification.id if typification else conv.typification_id
    await commit_and_broadcast(session, conv, "conversation.closed")

    from app.classifier import run_in_background

    if (await get_setting(session, "classifier", conv.organization_id))["classify_on_close"]:
        await run_in_background(conv.id, "close")
    if actor != "flow":
        from app.flows.engine import on_event

        await on_event(conv.id, "close")
    __import__("app.quality.hooks", fromlist=["on_close"]).on_close(conv.id)  # QA automático (nunca lanza)
    __import__("app.golden.hooks", fromlist=["on_close"]).on_close(conv.id)  # llaves del cliente (nunca lanza)


# --- Alertas --------------------------------------------------------------------
async def create_alert(session: AsyncSession, org: int, **fields) -> Alert:
    alert = Alert(organization_id=org, **fields)
    session.add(alert)
    await session.commit()
    await hub.broadcast("alert.new", {"id": alert.id, "title": alert.title, "severity": alert.severity}, org)
    return alert
