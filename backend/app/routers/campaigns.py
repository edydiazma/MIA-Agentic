import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import templates
from app.auth import current_agent, require_admin
from app.campaigns import run_campaign
from app.db import get_session
from app.models import Agent, Campaign, CampaignRecipient, Channel, Contact, ContactTag, Tag, utcnow
from app.routers.contacts import normalize_phone
from app.schemas import UTCDateTime
from app.plans import enforce_limit

router = APIRouter(prefix="/api", tags=["campaigns"])
_running: set[asyncio.Task] = set()


class CampaignIn(BaseModel):
    name: str
    template_name: str
    template_language: str
    params: list[str] = []
    tag: str | None = None  # audiencia por etiqueta
    segment_id: int | None = None  # o por segmento (§21.1)
    phones: list[str] = []  # o lista de números
    channel_id: int | None = None


class CampaignOut(BaseModel):
    id: int
    name: str
    template_name: str
    template_language: str
    params: list[str]
    audience: dict = {}
    status: str
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    total: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0
    skipped: int = 0
    not_delivered: int = 0  # enviados que Meta nunca confirmó como entregados


async def channel_for(session: AsyncSession, org: int, channel_id: int | None = None) -> Channel:
    stmt = select(Channel).where(Channel.organization_id == org, Channel.provider == "whatsapp_cloud")  # plantillas: solo WhatsApp
    stmt = stmt.where(Channel.id == channel_id) if channel_id else stmt.order_by(Channel.id).limit(1)
    ch = (await session.scalars(stmt)).first()
    if not ch:
        raise HTTPException(422, "No hay un número de WhatsApp configurado")
    return ch


async def campaign_stats(session: AsyncSession, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    s = CampaignRecipient.status

    def n(cond):
        return func.sum(case((cond, 1), else_=0))

    rows = (await session.execute(
        select(CampaignRecipient.campaign_id, func.count(),
               n(s.in_(["sent", "delivered", "read"])), n(s.in_(["delivered", "read"])), n(s == "read"),
               n(s == "failed"), n(s == "skipped"), n(s == "sent"))
        .where(CampaignRecipient.campaign_id.in_(ids)).group_by(CampaignRecipient.campaign_id))).all()
    return {r[0]: {"total": r[1], "sent": r[2] or 0, "delivered": r[3] or 0, "read": r[4] or 0, "failed": r[5] or 0,
                   "skipped": r[6] or 0, "not_delivered": r[7] or 0} for r in rows}


def _out(c: Campaign, stats: dict) -> CampaignOut:
    return CampaignOut(id=c.id, name=c.name, template_name=c.template_name, template_language=c.template_language,
                       params=c.params or [], audience=c.audience or {}, status=c.status, created_at=c.created_at,
                       started_at=c.started_at, finished_at=c.finished_at, **stats.get(c.id, {}))


async def _campaign(session: AsyncSession, cid: int, agent: Agent) -> Campaign:
    c = await session.get(Campaign, cid)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Campaña no encontrada")
    return c


@router.get("/templates")
async def list_templates(refresh: bool = False, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    channel = await channel_for(session, agent.organization_id)
    try:
        return await templates.list_templates(session, channel, refresh=refresh)
    except Exception as e:
        raise HTTPException(502, f"No se pudieron obtener las plantillas de Meta: {e}") from e


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(days: int | None = Query(default=None, le=3650), agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    stmt = select(Campaign).where(Campaign.organization_id == agent.organization_id).order_by(Campaign.id.desc()).limit(200)
    if days:
        stmt = stmt.where(Campaign.created_at >= utcnow() - timedelta(days=days))
    rows = (await session.scalars(stmt)).all()
    stats = await campaign_stats(session, [c.id for c in rows])
    return [_out(c, stats) for c in rows]


@router.post("/campaigns", response_model=CampaignOut)
async def create_campaign(body: CampaignIn, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    channel = await channel_for(session, org, body.channel_id)
    if body.segment_id:
        from app import segments
        from app.models import Segment

        segment = await session.get(Segment, body.segment_id)
        if segment is None or segment.organization_id != org:
            raise HTTPException(404, "Segmento no encontrado")
        try:
            ids = await segments.segment_contact_ids(session, segment)
        except segments.SegmentError as e:
            raise HTTPException(422, str(e)) from e
        contacts = list((await session.scalars(select(Contact).where(
            Contact.organization_id == org, Contact.id.in_(ids or [0]), Contact.blocked.is_(False))))
            .all())
        audience = {"segment_id": segment.id, "segment": segment.name}
    elif body.tag:
        tag = await session.scalar(select(Tag).where(Tag.organization_id == org, Tag.name == body.tag.strip().lower()))
        contacts = list((await session.scalars(
            select(Contact).join(ContactTag, ContactTag.contact_id == Contact.id)
            .where(ContactTag.tag_id == (tag.id if tag else 0), Contact.blocked.is_(False)))).all())
        audience = {"tag": body.tag.strip().lower(), "tag_ids": [tag.id] if tag else []}
    else:
        wa_ids = {p for p in (normalize_phone(x) for x in body.phones) if p}
        contacts = list((await session.scalars(
            select(Contact).where(Contact.organization_id == org, Contact.wa_id.in_(wa_ids or {""})))).all())
        for wa_id in wa_ids - {c.wa_id for c in contacts}:
            c = Contact(organization_id=org, wa_id=wa_id)
            session.add(c)
            contacts.append(c)
        await session.flush()
        audience = {"phones": sorted(wa_ids)}
    if not contacts:
        raise HTTPException(422, "La audiencia está vacía")

    campaign = Campaign(organization_id=org, name=body.name, channel_id=channel.id, template_name=body.template_name,
                        template_language=body.template_language, params=body.params, audience=audience,
                        created_by=agent.id)
    session.add(campaign)
    await session.flush()
    session.add_all([CampaignRecipient(campaign_id=campaign.id, contact_id=c.id) for c in contacts])
    await session.commit()
    return _out(campaign, await campaign_stats(session, [campaign.id]))


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(campaign_id: int, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    campaign = await _campaign(session, campaign_id, agent)
    if campaign.status not in ("draft", "scheduled"):
        raise HTTPException(409, "La campaña ya se envió")
    pending = await session.scalar(select(func.count()).where(
        CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.status == "pending")) or 0
    await enforce_limit(session, agent.organization_id, "campaign_recipients_month", needed=pending)
    campaign.status = "running"
    await session.commit()
    task = asyncio.create_task(run_campaign(campaign.id))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return _out(campaign, await campaign_stats(session, [campaign.id]))


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: int, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    campaign = await _campaign(session, campaign_id, agent)
    recipients = (await session.scalars(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id).order_by(CampaignRecipient.id))).unique().all()
    return {
        **_out(campaign, await campaign_stats(session, [campaign_id])).model_dump(mode="json"),
        "recipients": [{"id": r.id, "contact_id": r.contact_id, "wa_id": r.contact.wa_id, "name": r.contact.name,
                        "status": r.status, "error": r.error, "error_code": r.error_code,
                        "sent_at": r.sent_at, "delivered_at": r.delivered_at, "read_at": r.read_at}
                       for r in recipients],
    }


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: int, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    campaign = await _campaign(session, campaign_id, agent)
    if campaign.status != "draft":
        raise HTTPException(409, "Solo se pueden borrar borradores")
    await session.execute(delete(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id))
    await session.delete(campaign)
    await session.commit()
    return {"ok": True}
