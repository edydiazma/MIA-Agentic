"""Acciones del asesor hacia el cliente: iniciar conversación, plantilla masiva desde la lista de clientes y enviar
respuestas rápidas (texto con variables + adjuntos). docs/data-model.md §18.3."""

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage, templates
from app.auth import current_agent
from app.campaigns import run_campaign, send_template_message
from app.db import get_session, set_actor
from app.identity import wa_address
from app.models import (
    Agent,
    Campaign,
    CampaignRecipient,
    Channel,
    Contact,
    Conversation,
    Group,
    Organization,
    QuickReply,
    Resource,
)
from app.plans import enforce_limit
from app.service import (
    commit_and_broadcast,
    conversation_out,
    get_conversation,
    message_out,
    reload,
    send_text,
    within_session_window,
)

router = APIRouter(prefix="/api", tags=["outreach"])
_running: set[asyncio.Task] = set()
VAR_RE = re.compile(r"\{\{\s*(agent_name|client_name|client_first_name|group_name|company_name)\s*\}\}")


# --- Variables de respuestas rápidas --------------------------------------------------------------------------
def render(text: str, *, agent_name: str = "", client_name: str = "", group_name: str = "",
           company_name: str = "") -> str:
    values = {"agent_name": agent_name, "client_name": client_name,
              "client_first_name": (client_name or "").split(" ")[0], "group_name": group_name,
              "company_name": company_name}
    return VAR_RE.sub(lambda m: values[m.group(1)], text or "")


async def render_for(session: AsyncSession, text: str, agent: Agent, conv: Conversation | None) -> str:
    org = await session.get(Organization, agent.organization_id)
    group = await session.get(Group, conv.group_id) if conv and conv.group_id else None
    return render(text, agent_name=agent.name, client_name=(conv.contact.name or "") if conv else "",
                  group_name=group.name if group else "", company_name=org.name if org else "")


def available_for(q: QuickReply, group_id: int | None) -> bool:
    return q.is_active and (not q.group_ids or (group_id is not None and group_id in q.group_ids))


def qr_out(q: QuickReply, rendered: str | None = None, resources: dict[int, Resource] | None = None) -> dict:
    res = resources or {}
    return {"id": q.id, "shortcut": q.shortcut, "title": q.title, "category": q.category, "text": q.text,
            "rendered": rendered if rendered is not None else q.text, "resource_ids": list(q.resource_ids or []),
            "attachments": [{"id": r.id, "name": r.name, "mime": r.mime} for i in q.resource_ids or []
                            if (r := res.get(i))],
            "group_ids": list(q.group_ids or []), "is_active": q.is_active, "usage_count": q.usage_count,
            "updated_at": q.updated_at}


async def quick_replies_for(session: AsyncSession, agent: Agent, q: str | None,
                            conversation_id: int | None, include_inactive: bool = False) -> list[dict]:
    conv = None
    if conversation_id:
        conv = await get_conversation(session, conversation_id, agent.organization_id)
        if not conv:
            raise HTTPException(404, "Conversación no encontrada")
    stmt = select(QuickReply).where(QuickReply.organization_id == agent.organization_id)
    if q:
        like = f"%{q.strip().lstrip('/')}%"
        stmt = stmt.where(or_(QuickReply.shortcut.ilike(like), QuickReply.title.ilike(like),
                              QuickReply.category.ilike(like), QuickReply.text.ilike(like)))
    rows = (await session.scalars(stmt.order_by(QuickReply.usage_count.desc(), QuickReply.shortcut))).all()
    if conv is not None:
        rows = [r for r in rows if available_for(r, conv.group_id)]
    elif not include_inactive:
        rows = [r for r in rows if r.is_active]
    ids = {i for r in rows for i in r.resource_ids or []}
    resources = {r.id: r for r in (await session.scalars(select(Resource).where(
        Resource.organization_id == agent.organization_id, Resource.id.in_(ids or {0})))).all()}
    out = []
    for r in rows:
        rendered = await render_for(session, r.text, agent, conv) if conv else None
        out.append(qr_out(r, rendered, resources))
    return out


class QuickSendIn(BaseModel):
    text: str | None = None  # texto editado por el asesor (si no, el de la respuesta con variables)


@router.post("/conversations/{conv_id}/quick-replies/{qid}/send")
async def send_quick_reply(conv_id: int, qid: int, body: QuickSendIn | None = None,
                           agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    from app.routers.inbox import _require_window, _send_file, _take

    conv = await get_conversation(session, conv_id, agent.organization_id)
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")
    q = await session.get(QuickReply, qid)
    if not q or q.organization_id != agent.organization_id or not available_for(q, conv.group_id):
        raise HTTPException(404, "Respuesta rápida no disponible para esta conversación")
    _require_window(conv)
    await _take(session, conv, agent)
    text = (body.text if body and body.text is not None else await render_for(session, q.text, agent, conv)).strip()
    sent = []
    if text:
        sent += [message_out(m) for m in await send_text(session, conv, text, sender_type="agent", agent_id=agent.id)]
    for rid in q.resource_ids or []:
        res = await session.get(Resource, rid)
        if res and res.organization_id == agent.organization_id:
            msg = await _send_file(session, conv, agent, await storage.download(res.storage_path), res.mime,
                                   res.name, None)
            sent.append(message_out(msg))
    q.usage_count = (q.usage_count or 0) + 1
    await session.commit()
    return {"messages": sent}


# --- Iniciar una conversación desde la ficha / lista de clientes ----------------------------------------------
class StartIn(BaseModel):
    contact_id: int
    channel_id: int | None = None
    text: str | None = None  # solo dentro de la ventana de atención
    template_name: str | None = None
    language: str | None = None
    params: list[str] = []
    bot: str = "off"  # on: responde el bot; off: queda con un asesor
    assign_to_me: bool = True
    agent_id: int | None = None
    group_id: int | None = None
    mode: str = "continue"  # continue: la última conversación del canal; new: una conversación nueva


async def _channel(session: AsyncSession, org: int, channel_id: int | None) -> Channel:
    if channel_id:
        ch = await session.get(Channel, channel_id)
        if not ch or ch.organization_id != org:
            raise HTTPException(404, "Canal no encontrado")
        return ch
    ch = (await session.scalars(select(Channel).where(Channel.organization_id == org,
                                                      Channel.provider == "whatsapp_cloud").order_by(Channel.id)
                                .limit(1))).first()
    if not ch:
        raise HTTPException(409, "No hay un número de WhatsApp conectado")
    return ch


async def _template(session: AsyncSession, channel: Channel, name: str, language: str | None) -> dict:
    if channel.provider != "whatsapp_cloud":
        raise HTTPException(409, "Las plantillas son solo de WhatsApp")
    catalog = await templates.list_templates(session, channel)
    tpl = next((t for t in catalog if t["name"] == name and (not language or t["language"] == language)), None)
    if not tpl or tpl["status"] != "APPROVED":
        raise HTTPException(404, "Plantilla no encontrada o no aprobada")
    if not tpl["supported"]:
        raise HTTPException(422, tpl["unsupported_reason"])
    return tpl


async def apply_assignment(session: AsyncSession, conv: Conversation, *, bot: str, agent_id: int | None,
                           group_id: int | None) -> None:
    """bot on → responde el bot; bot off → asesor (o cola del grupo si no hay asesor)."""
    if group_id:
        conv.group_id = group_id
    if bot == "on":
        conv.status, conv.assigned_agent_id = "bot", None
    else:
        conv.status = "human"
        if agent_id:
            conv.assigned_agent_id = agent_id


@router.post("/contacts/start-conversation")
async def start_conversation(body: StartIn, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    if body.bot not in ("on", "off") or body.mode not in ("continue", "new"):
        raise HTTPException(422, "bot debe ser on|off y mode continue|new")
    contact = await session.get(Contact, body.contact_id)
    if not contact or contact.organization_id != org:
        raise HTTPException(404, "Cliente no encontrado")
    if contact.blocked:
        raise HTTPException(409, "El cliente está bloqueado")
    channel = await _channel(session, org, body.channel_id)
    if channel.provider == "whatsapp_cloud" and not wa_address(contact):
        raise HTTPException(409, "El cliente no tiene número ni usuario de WhatsApp")
    target_agent = body.agent_id or (agent.id if body.assign_to_me and body.bot == "off" else None)
    for model, value, label in ((Agent, target_agent, "Asesor"), (Group, body.group_id, "Grupo")):
        if value is not None:
            row = await session.get(model, value)
            if not row or row.organization_id != org:
                raise HTTPException(404, f"{label} no encontrado")

    latest = (await session.scalars(select(Conversation).where(
        Conversation.contact_id == contact.id, Conversation.channel_id == channel.id)
        .order_by(Conversation.id.desc()).limit(1))).first()
    window_open = bool(latest and within_session_window(latest, human=True))
    if not body.template_name and not (body.text or "").strip():
        raise HTTPException(422, "Escribe un mensaje o elige una plantilla")
    if not body.template_name and not window_open:
        raise HTTPException(409, "El cliente no ha escrito en las últimas 24 h: WhatsApp exige una plantilla aprobada")
    tpl = await _template(session, channel, body.template_name, body.language) if body.template_name else None
    if tpl and tpl["category"] == "MARKETING" and contact.marketing_opt_out:
        raise HTTPException(409, "El cliente pidió no recibir marketing: usa una plantilla de utilidad")

    await set_actor(session, "agent", agent.id)
    if latest is None or body.mode == "new":
        conv = Conversation(organization_id=org, contact_id=contact.id, channel_id=channel.id,
                            ai_agent_id=channel.default_ai_agent_id, status="bot")
        session.add(conv)
        await session.flush()
        conv = await reload(session, conv)
    else:
        conv = latest
        if conv.status == "closed":
            conv.status, conv.assigned_agent_id = "bot", None
            await session.flush()
            conv = await reload(session, conv)
    await apply_assignment(session, conv, bot=body.bot, agent_id=target_agent, group_id=body.group_id)
    await commit_and_broadcast(session, conv)

    if tpl:
        msg = await send_template_message(session, conv, tpl, templates.personalize(body.params, contact.name),
                                          sender_type="agent", agent_id=agent.id)
        messages = [msg]
    else:
        messages = await send_text(session, conv, body.text.strip(), sender_type="agent", agent_id=agent.id)
    await session.commit()
    conv = await get_conversation(session, conv.id, org)
    return {"conversation": conversation_out(conv), "messages": [message_out(m) for m in messages],
            "window_open": window_open}


# --- Plantilla masiva desde la selección de clientes ----------------------------------------------------------
class SelectionIn(BaseModel):
    contact_ids: list[int] = []
    filter: dict | None = None  # mismos filtros que GET /api/contacts (q, tag, channel_ids, agent_id…)
    channel_id: int | None = None
    template_name: str
    template_language: str
    params: list[str] = []
    name: str | None = None
    # {assign_agent_id, assign_group_id, bot: on|off, conversation: continue|new, tags: [...]}
    options: dict = {}
    start: bool = True


FILTER_TYPES = {"q": str, "tag": str, "tags": str, "stage": str, "channels": str, "channel_ids": str,
                "agent_id": int, "typification_id": int, "created_from": "date", "created_to": "date",
                "updated_from": "date", "updated_to": "date", "last_interaction_from": "date",
                "last_interaction_to": "date", "inactive_days_gte": float, "has_products": bool, "product": str,
                "source_channel": str, "source_ad_id": str, "source_campaign": str}


def _coerce_filter(raw: dict) -> dict:
    """El filtro llega como en la URL de GET /api/contacts (texto): se convierte a los tipos de ContactFilters."""
    from datetime import date

    out: dict = {}
    for k, v in (raw or {}).items():
        kind = FILTER_TYPES.get(k)
        if kind is None or v is None or v == "":
            continue
        try:
            if kind == "date":
                out[k] = v if isinstance(v, date) else date.fromisoformat(str(v))
            elif kind is bool:
                out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "si", "sí")
            else:
                out[k] = kind(v)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"Filtro inválido: {k}") from e
    return out


@router.post("/campaigns/from-selection")
async def campaign_from_selection(body: SelectionIn, agent: Agent = Depends(current_agent),
                                  session: AsyncSession = Depends(get_session)):
    from app.routers.contacts import ContactFilters, _scoped

    org = agent.organization_id
    channel = await _channel(session, org, body.channel_id)
    await _template(session, channel, body.template_name, body.template_language)
    if body.contact_ids:
        conds = await _scoped(session, agent, [Contact.organization_id == org, Contact.id.in_(set(body.contact_ids))])
    elif body.filter is not None:
        f = ContactFilters(**_coerce_filter(body.filter), sort=None, order="desc")
        conds = await _scoped(session, agent, f.where(org))
    else:
        raise HTTPException(422, "Selecciona clientes o envía un filtro")
    contact_ids = list((await session.scalars(select(Contact.id).where(*conds, Contact.blocked.is_(False)))).all())
    if not contact_ids:
        raise HTTPException(422, "La selección está vacía")
    opts = {k: v for k, v in (body.options or {}).items()
            if k in ("assign_agent_id", "assign_group_id", "bot", "conversation", "tags")}
    if opts.get("bot") not in (None, "on", "off") or opts.get("conversation") not in (None, "continue", "new"):
        raise HTTPException(422, "Opciones inválidas")
    campaign = Campaign(organization_id=org, name=body.name or f"Selección de clientes · {len(contact_ids)}",
                        channel_id=channel.id, template_name=body.template_name,
                        template_language=body.template_language, params=body.params,
                        audience={"contact_ids": contact_ids}, created_by=agent.id, source="client_list", options=opts)
    session.add(campaign)
    await session.flush()
    session.add_all([CampaignRecipient(campaign_id=campaign.id, contact_id=cid) for cid in contact_ids])
    await session.commit()
    if body.start:
        pending = await session.scalar(select(func.count()).where(CampaignRecipient.campaign_id == campaign.id)) or 0
        await enforce_limit(session, org, "campaign_recipients_month", needed=pending)
        campaign.status = "running"
        await session.commit()
        task = asyncio.create_task(run_campaign(campaign.id))
        _running.add(task)
        task.add_done_callback(_running.discard)
    return {"id": campaign.id, "name": campaign.name, "status": campaign.status, "recipients": len(contact_ids),
            "source": campaign.source, "options": campaign.options}


@router.get("/quick-replies/available")
async def available_quick_replies(q: str | None = None, conversation_id: int | None = Query(default=None),
                                  agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await quick_replies_for(session, agent, q, conversation_id)
