"""Centro de control (Inicio), tablero en tiempo real y reportes.

Los reportes históricos leen los rollups de `reporting.*` (docs/data-model.md §4). Antes de leer se
recalcula solo la parte del rango que cae en hoy/ayer (barato), así funciona igual sin pg_cron.
Las medianas salen de los hechos (conversation_events), porque un rollup no puede dar medianas.
"""

import csv
import io
from urllib.parse import urlparse
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.permissions import require_permission
from app.db import get_session
from app.fields import custom_values, fields_by_key
from app.models import (
    Agent,
    AgentGroup,
    AgentSession,
    AgentStatus,
    AIConnection,
    AIConnectionHealth,
    Alert,
    Appointment,
    Campaign,
    Channel,
    Contact,
    Conversation,
    ConversationEvent,
    Flow,
    FlowVersion,
    FollowUp,
    Group,
    Message,
    Organization,
    OutboundWebhook,
    WaTemplate,
    utcnow,
)
from app.realtime import hub
from app.scope import agent_clause, conversation_clause, conversation_visible, scope_for
from app.flows.catalog import BLOCKS
from app.routers.campaigns import campaign_stats
from app.routers.config import integration_status
from app.settings_store import get_setting

router = APIRouter(prefix="/api", tags=["reports"])


# --- Rango y frescura -----------------------------------------------------------
async def _tz(session: AsyncSession, org: int) -> ZoneInfo:
    return ZoneInfo((await session.get(Organization, org)).timezone)


async def _range(session: AsyncSession, org: int, start: date | None, end: date | None):
    """(lo, hi, tz, start, end): fechas locales de la organización y sus límites en UTC."""
    tz = await _tz(session, org)
    today = utcnow().astimezone(tz).date()
    end = min(end or today, today)
    start = min(start or end - timedelta(days=6), end)
    lo = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    hi = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    # Recalcular solo lo que puede haber cambiado: hoy y ayer
    fresh_from = max(start, today - timedelta(days=1))
    if fresh_from <= end:
        await session.execute(text("select reporting.refresh_range(:o, :a, :b)"), {"o": org, "a": fresh_from, "b": end})
        await session.commit()
    return lo, hi, tz, start, end


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _pct(a: int | float, b: int | float) -> float | None:
    return round(100 * a / b, 1) if b else None


async def _rows(session: AsyncSession, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in (await session.execute(text(sql), params)).all()]


async def _first_response_seconds(session: AsyncSession, org: int, lo: datetime, hi: datetime) -> list[tuple]:
    """(assigned_agent_id, segundos) de cada primera respuesta en el rango (hechos)."""
    rows = (await session.execute(
        select(ConversationEvent.assigned_agent_id, ConversationEvent.payload["seconds"].as_integer())
        .where(ConversationEvent.organization_id == org, ConversationEvent.event_type == "first_agent_response",
               ConversationEvent.occurred_at >= lo, ConversationEvent.occurred_at < hi))).all()
    return [(a, s) for a, s in rows if s is not None]


# --- Inicio: Centro de control --------------------------------------------------
@router.get("/control-center")
async def control_center(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    now = utcnow()
    week, prev_week = now - timedelta(days=7), now - timedelta(days=14)

    alerts = (await session.scalars(select(Alert).where(Alert.organization_id == org, Alert.resolved_at.is_(None))
                                    .order_by(Alert.id.desc()).limit(50))).all()
    opt = Contact.organization_id == org
    opt_out_7 = await session.scalar(select(func.count()).where(opt, Contact.opt_out_at >= week)) or 0
    opt_out_prev = await session.scalar(
        select(func.count()).where(opt, Contact.opt_out_at >= prev_week, Contact.opt_out_at < week)) or 0
    channels = (await session.scalars(select(Channel).where(Channel.organization_id == org,
                                                            Channel.provider == "whatsapp_cloud"))).unique().all()
    template_total = await session.scalar(
        select(func.count()).where(WaTemplate.organization_id == org, WaTemplate.status == "APPROVED"))
    template_alerts = len({a.ref for a in alerts if a.layer == "template"})

    campaigns = (await session.scalars(select(Campaign).where(Campaign.organization_id == org,
                                                              Campaign.created_at >= week)
                                       .order_by(Campaign.id.desc()))).all()
    stats = await campaign_stats(session, [c.id for c in campaigns])
    camp_rows = [{"id": c.id, "name": c.name, "status": c.status, "created_at": c.created_at,
                  **{k: stats.get(c.id, {}).get(k, 0)
                     for k in ("total", "sent", "delivered", "failed", "not_delivered", "read")}} for c in campaigns]

    hooks = (await session.scalars(select(OutboundWebhook).where(OutboundWebhook.organization_id == org))).all()
    hooks_active = sum(1 for w in hooks if w.active)

    conns = (await session.execute(
        select(AIConnection.id, AIConnection.is_active, AIConnectionHealth.state)
        .outerjoin(AIConnectionHealth, AIConnectionHealth.connection_id == AIConnection.id)
        .where(AIConnection.organization_id == org))).all()

    integrations = await integration_status(session, org)

    tz = await _tz(session, org)
    today_end = datetime.combine(now.astimezone(tz).date() + timedelta(days=1), datetime.min.time(), tz)
    my_open = await session.scalar(select(func.count()).where(
        Conversation.organization_id == org, Conversation.assigned_agent_id == agent.id,
        Conversation.status == "human")) or 0
    waiting = await session.scalar(select(func.count()).where(
        Conversation.organization_id == org, Conversation.status == "human",
        Conversation.assigned_agent_id.is_(None))) or 0
    followups_due = await session.scalar(select(func.count()).where(
        FollowUp.organization_id == org, FollowUp.agent_id == agent.id, FollowUp.done_at.is_(None),
        FollowUp.due_at < today_end.astimezone(UTC))) or 0
    appts_today = await session.scalar(select(func.count()).where(
        Appointment.organization_id == org, Appointment.status == "scheduled", Appointment.starts_at >= now,
        Appointment.starts_at < today_end.astimezone(UTC))) or 0

    meta_alerts = [a for a in alerts if a.source == "meta"]
    return {
        "updated_at": now,
        "cards": {
            "meta": {"alerts": len(meta_alerts)},
            "campaigns": {"total_7d": len(campaigns), "failed": sum(1 for c in campaigns if c.status == "failed")},
            "integrations": {"connected": sum(1 for i in integrations if i["connected"]), "total": len(integrations),
                             "failing": sum(1 for i in integrations if i["last_error"])},
            "webhooks": {"total": len(hooks), "active": hooks_active,
                         "failing": sum(1 for w in hooks if w.active and w.consecutive_failures),
                         "disabled": len(hooks) - hooks_active},
            "ai": {"connections": sum(1 for c in conns if c.is_active),
                   "open": sum(1 for c in conns if c.is_active and c.state == "open"),
                   "half_open": sum(1 for c in conns if c.is_active and c.state == "half_open")},
        },
        "meta_summary": {
            "accounts_with_alert": len({a.ref for a in alerts if a.layer in ("account", "phone")}),
            "accounts_total": len(channels),
            "templates_with_alert": template_alerts if template_total else None,
            "templates_total": template_total or None,
            "opt_out_7d": opt_out_7,
            "opt_out_prev_7d": opt_out_prev,
        },
        "alerts": [{"id": a.id, "severity": a.severity, "layer": a.layer, "source": a.source, "title": a.title,
                    "description": a.description, "ref": a.ref, "created_at": a.created_at} for a in alerts],
        "campaigns": camp_rows,
        "me": {"open": my_open, "waiting_unassigned": waiting, "followups_due": followups_due,
               "appointments_today": appts_today},
    }


# --- Tablero en tiempo real (estado actual) -------------------------------------
@router.get("/reports/realtime")
async def realtime(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    now = utcnow()
    scope = await scope_for(session, agent)
    clause = conversation_clause(scope)
    stmt = select(Conversation.status, Conversation.assigned_agent_id, Conversation.group_id, Conversation.unread_count,
                  Conversation.handoff_at, Conversation.last_message_at) \
        .where(Conversation.organization_id == org, Conversation.status != "closed")
    open_rows = (await session.execute(stmt if clause is None else stmt.where(clause))).all()
    by_status = Counter(r.status for r in open_rows)
    waiting = [r for r in open_rows if r.status == "human" and not r.assigned_agent_id]
    waits = [(now - (r.handoff_at or r.last_message_at)).total_seconds() / 60 for r in waiting]
    oldest = max(waits, default=0)

    online = hub.online_agent_ids()
    a_stmt = select(Agent).where(Agent.organization_id == org, Agent.is_active).order_by(Agent.name)
    a_clause = agent_clause(scope)
    agents = (await session.scalars(a_stmt if a_clause is None else a_stmt.where(a_clause))).all()
    g_stmt = select(Group).where(Group.organization_id == org)
    if not scope.unrestricted:
        g_stmt = g_stmt.where(Group.id.in_(sorted(scope.group_ids) or [0]))
    groups = {g.id: g.name for g in (await session.scalars(g_stmt)).all()}
    memberships = defaultdict(list)
    group_members: dict[int, set[int]] = defaultdict(set)
    for a_id, g_id in (await session.execute(select(AgentGroup.agent_id, AgentGroup.group_id)
                                             .where(AgentGroup.group_id.in_(list(groups) or [0])))).all():
        memberships[a_id].append(groups[g_id])
        group_members[g_id].add(a_id)
    load = Counter(r.assigned_agent_id for r in open_rows if r.status == "human" and r.assigned_agent_id)
    unread = Counter()
    for r in open_rows:
        if r.assigned_agent_id:
            unread[r.assigned_agent_id] += r.unread_count

    # Estado personalizado y tiempo en el estado actual (§18.1)
    statuses = {st.id: st for st in (await session.scalars(
        select(AgentStatus).where(AgentStatus.organization_id == org))).all()}

    def status_of(a: Agent) -> dict | None:
        st = statuses.get(getattr(a, "status_id", None))
        if st is None:
            return None
        since = getattr(a, "status_changed_at", None)
        return {"key": st.key, "name": st.name, "color": st.color, "icon": st.icon,
                "receives_conversations": st.receives_conversations,
                "seconds": int((now - since).total_seconds()) if since else None}

    # Contadores desde las 00:00 (zona de la empresa)
    tz = await _tz(session, org)
    today = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
    c = Conversation
    base = [c.organization_id == org]
    if clause is not None:
        base.append(clause)
    t = (await session.execute(select(
        func.count().filter(c.created_at >= today),
        func.count().filter(c.handoff_at >= today),
        func.count().filter(c.closed_at >= today),
        func.count().filter(c.first_response_at >= today),
        func.count().filter(c.closed_at >= today, c.handoff_at.is_not(None), c.first_response_at.is_(None),
                            c.assigned_agent_id.is_(None)),
        func.avg(func.extract("epoch", c.first_response_at - c.handoff_at))
            .filter(c.first_response_at >= today, c.first_response_at >= c.handoff_at),
    ).where(*base))).one()
    group_rows = []
    for gid, name in sorted(groups.items(), key=lambda kv: kv[1]):
        q = [w for w in waiting if w.group_id == gid]
        g_waits = [(now - (w.handoff_at or w.last_message_at)).total_seconds() / 60 for w in q]
        members = group_members.get(gid, set())
        group_rows.append({"group_id": gid, "name": name, "agents": len(members),
                           "online": len(members & online), "queue": len(q),
                           "open": sum(1 for r in open_rows if r.group_id == gid and r.status == "human"),
                           "longest_wait_minutes": round(max(g_waits, default=0), 1)})
    return {
        "updated_at": now,
        "by_status": {"bot": by_status["bot"], "human": by_status["human"]},
        "waiting_unassigned": len(waiting),
        "oldest_wait_minutes": round(oldest, 1),
        "avg_wait_minutes_now": round(sum(waits) / len(waits), 1) if waits else 0,
        "today": {"incoming": t[0], "handoffs": t[1], "closed": t[2], "attended": t[3], "abandoned": t[4],
                  "avg_first_response_s": int(t[5]) if t[5] is not None else None},
        "agents": [{"id": a.id, "name": a.name, "online": a.id in online, "availability": a.availability,
                    "status": status_of(a), "groups": memberships.get(a.id, []), "open": load.get(a.id, 0),
                    "unread": unread.get(a.id, 0)}
                   for a in agents],
        "by_group": dict(Counter(groups.get(r.group_id, "Sin grupo") for r in open_rows if r.status == "human")),
        "groups": group_rows,
    }


# --- Reporte general (rollups) --------------------------------------------------
@router.get("/reports/general")
async def general(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, start, end = await _range(session, org, start, end)
    rows = {r["day"]: r for r in await _rows(session, """
        select * from reporting.daily_conversations where organization_id = :o and day between :a and :b""",
        o=org, a=start, b=end)}
    tot = Counter()
    for r in rows.values():
        for k, v in r.items():
            if isinstance(v, int) and k != "organization_id":
                tot[k] += v
    sla = (await get_setting(session, "conversations", org))["sla_minutes"]
    fr = [s for _a, s in await _first_response_seconds(session, org, lo, hi)]

    typ = await _rows(session, """
        select t.name, sum(d.closed)::int as closed, sum(d.ai_compared)::int as compared, sum(d.ai_agreed)::int as agreed
        from reporting.daily_typifications d join public.typifications t on t.id = d.typification_id
        where d.organization_id = :o and d.day between :a and :b group by t.name order by 2 desc""",
        o=org, a=start, b=end)
    tags = await _rows(session, """
        select t.name, sum(d.conversations)::int as n from reporting.daily_tags d join public.tags t on t.id = d.tag_id
        where d.organization_id = :o and d.day between :a and :b group by t.name order by 2 desc""",
        o=org, a=start, b=end)
    disagreements = await _rows(session, """
        select ai.name || ' → ' || t.name as label, count(*)::int as n
        from public.conversations c join public.typifications t on t.id = c.typification_id
        join public.typifications ai on ai.id = c.ai_typification_id
        where c.organization_id = :o and c.closed_at >= :lo and c.closed_at < :hi and c.typification_id <> c.ai_typification_id
        group by 1 order by 2 desc limit 10""", o=org, lo=lo, hi=hi)
    classified = await session.scalar(select(func.count()).where(
        Conversation.organization_id == org, Conversation.ai_classified_at >= lo, Conversation.ai_classified_at < hi))
    compared = sum(t["compared"] for t in typ)
    unclassified_closed = tot["closed"] - sum(t["closed"] for t in typ)
    typifications = {t["name"]: t["closed"] for t in typ}
    if unclassified_closed > 0:
        typifications["Sin tipificar"] = unclassified_closed

    return {
        "range": {"start": lo, "end": hi},
        "totals": {
            "new_conversations": tot["new_conversations"],
            "inbound_messages": tot["inbound_messages"],
            "bot_messages": tot["bot_messages"],
            "agent_messages": tot["agent_messages"],
            "campaign_messages": tot["campaign_messages"],
            "flow_messages": tot["flow_messages"],
            "handoffs": tot["handoffs"],
            "closed": tot["closed"],
            "sales": tot["sales"],
            "ad_conversations": tot["ad_conversations"],
            "bot_resolution_pct": _pct(tot["closed_bot_only"], tot["closed"]),
            "first_response_median_min": round(median(fr) / 60, 1) if fr else None,
            "sla_minutes": sla,
            "sla_pct": _pct(tot["first_response_within_sla"], tot["first_responses"]),
        },
        "typifications": typifications,
        "tags": {t["name"]: t["n"] for t in tags},
        "ai": {
            "classified": classified or 0,
            "compared": compared,
            "agreement_pct": _pct(sum(t["agreed"] for t in typ), compared),
            "disagreements": [[d["label"], d["n"]] for d in disagreements],
        },
        "series": [{"day": d.isoformat(), "inbound": rows.get(d, {}).get("inbound_messages", 0),
                    "bot": rows.get(d, {}).get("bot_messages", 0), "agent": rows.get(d, {}).get("agent_messages", 0),
                    "campaign": rows.get(d, {}).get("campaign_messages", 0),
                    "new_conversations": rows.get(d, {}).get("new_conversations", 0),
                    "handoffs": rows.get(d, {}).get("handoffs", 0)} for d in _days(start, end)],
    }


@router.get("/reports/stages")
async def stages(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Contact.stage, func.count()).where(
        Contact.organization_id == agent.organization_id, Contact.blocked.is_(False)).group_by(Contact.stage))).all()
    return dict(rows)


# --- Inbound --------------------------------------------------------------------
@router.get("/reports/inbound")
async def inbound(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, start, end = await _range(session, org, start, end)
    by_type = dict((await session.execute(
        select(Message.type, func.count()).where(Message.organization_id == org, Message.direction == "in",
                                                 Message.created_at >= lo, Message.created_at < hi)
        .group_by(Message.type))).all())
    hourly = await _rows(session, """
        select hour, sum(messages)::int as n from reporting.hourly_messages
        where organization_id = :o and direction = 'in' and hour >= :lo and hour < :hi group by hour""",
        o=org, lo=lo, hi=hi)
    by_hour = [0] * 24
    for h in hourly:
        by_hour[h["hour"].astimezone(tz).hour] += h["n"]
    tot = (await _rows(session, """
        select coalesce(sum(new_conversations), 0)::int as convs, coalesce(sum(ad_conversations), 0)::int as ads,
               coalesce(sum(bot_messages), 0)::int as bot_msgs, coalesce(sum(handoffs), 0)::int as handoffs
        from reporting.daily_conversations where organization_id = :o and day between :a and :b""",
        o=org, a=start, b=end))[0]
    bot_convs = await session.scalar(select(func.count(func.distinct(Message.conversation_id))).where(
        Message.organization_id == org, Message.sender_type == "bot", Message.created_at >= lo, Message.created_at < hi))
    reasons = await _rows(session, """
        select left(coalesce(payload->>'reason', 'Sin motivo'), 80) as reason, count(*)::int as n
        from public.conversation_events where organization_id = :o and event_type = 'handoff'
          and occurred_at >= :lo and occurred_at < :hi group by 1 order by 2 desc limit 10""", o=org, lo=lo, hi=hi)
    return {
        "messages": sum(by_type.values()),
        "by_type": by_type,
        "by_hour": by_hour,
        "conversations": tot["convs"],
        "by_source": {"ads": tot["ads"], "organic": tot["convs"] - tot["ads"]},
        "bots": {"messages": tot["bot_msgs"], "conversations": bot_convs or 0, "handoffs": tot["handoffs"],
                 "handoff_pct": _pct(tot["handoffs"], bot_convs or 0),
                 "top_handoff_reasons": [[r["reason"], r["n"]] for r in reasons]},
    }


@router.get("/reports/ctwa")
async def click_to_whatsapp(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                            session: AsyncSession = Depends(get_session)):
    """Conversaciones originadas en anuncios Click to WhatsApp de Meta."""
    org = agent.organization_id
    lo, hi, _tz, _s, _e = await _range(session, org, start, end)
    rows = await _rows(session, """
        select c.ad_source_id, max(c.ad_headline) as headline, max(c.ad_source_type) as source_type,
               max(c.ad_source_url) as url, count(*)::int as conversations,
               count(*) filter (where c.handoff_at is not null)::int as handoffs,
               count(*) filter (where t.is_success)::int as sales
        from public.conversations c left join public.typifications t on t.id = c.typification_id
        join public.channels ch on ch.id = c.channel_id and ch.provider = 'whatsapp_cloud'  -- Messenger/IG: Canales
        where c.organization_id = :o and c.ad_source_type is not null and c.last_message_at >= :lo and c.created_at < :hi
        group by c.ad_source_id order by 5 desc""", o=org, lo=lo, hi=hi)
    return {"total": sum(r["conversations"] for r in rows),
            "ads": [{"ad_id": r["ad_source_id"], "headline": r["headline"], "source_type": r["source_type"],
                     "url": r["url"], "conversations": r["conversations"], "handoffs": r["handoffs"],
                     "sales": r["sales"]} for r in rows]}


# --- Outbound -------------------------------------------------------------------
@router.get("/reports/outbound")
async def outbound(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                   session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, _tz, _s, _e = await _range(session, org, start, end)
    base = (Message.organization_id == org, Message.direction == "out", Message.sender_type != "system",
            Message.created_at >= lo, Message.created_at < hi)
    by_sender = dict((await session.execute(select(Message.sender_type, func.count()).where(*base)
                                            .group_by(Message.sender_type))).all())
    by_status = dict((await session.execute(select(Message.status, func.count()).where(*base)
                                            .group_by(Message.status))).all())
    individual: dict[str, Counter] = defaultdict(Counter)
    for tpl, status, n in (await session.execute(
            select(Message.template_name, Message.status, func.count())
            .where(*base, Message.type == "template", Message.campaign_id.is_(None))
            .group_by(Message.template_name, Message.status))).all():
        individual[tpl or "?"][status] += n
        individual[tpl or "?"]["total"] += n
    campaigns = (await session.scalars(select(Campaign).where(
        Campaign.organization_id == org, Campaign.created_at >= lo, Campaign.created_at < hi)
        .order_by(Campaign.id.desc()))).all()
    stats = await campaign_stats(session, [c.id for c in campaigns])
    return {
        "total": sum(by_sender.values()),
        "by_sender": by_sender,
        "by_status": by_status,
        "individual_templates": [{"template": k, **v} for k, v in sorted(individual.items())],
        "campaigns": [{"id": c.id, "name": c.name, "template": c.template_name, "status": c.status,
                       **stats.get(c.id, {})} for c in campaigns],
    }


# --- Asesores (nivel de servicio) -----------------------------------------------
@router.get("/reports/agents")
async def agents_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, _tz, start, end = await _range(session, org, start, end)
    sla = (await get_setting(session, "conversations", org))["sla_minutes"]
    roll = {r["agent_id"]: r for r in await _rows(session, """
        select agent_id, sum(closed)::int as closed, sum(sales)::int as sales, sum(messages_sent)::int as messages_sent,
               sum(first_responses)::int as first_responses, sum(first_response_within_sla)::int as within_sla
        from reporting.daily_agents where organization_id = :o and day between :a and :b group by agent_id""",
        o=org, a=start, b=end)}
    fr = await _first_response_seconds(session, org, lo, hi)
    per_agent: dict[int, list[int]] = defaultdict(list)
    for a_id, s in fr:
        if a_id:
            per_agent[a_id].append(s)
    handoffs = await session.scalar(select(func.count()).where(
        ConversationEvent.organization_id == org, ConversationEvent.event_type == "handoff",
        ConversationEvent.occurred_at >= lo, ConversationEvent.occurred_at < hi)) or 0
    online = hub.online_agent_ids()
    a_clause = agent_clause(await scope_for(session, agent))
    a_stmt = select(Agent).where(Agent.organization_id == org).order_by(Agent.name)
    agents = (await session.scalars(a_stmt if a_clause is None else a_stmt.where(a_clause))).all()
    rows = []
    for a in agents:
        r = roll.get(a.id, {})
        times = per_agent.get(a.id, [])
        rows.append({
            "id": a.id, "name": a.name, "online": a.id in online, "availability": a.availability,
            "is_active": a.is_active, "messages_sent": r.get("messages_sent", 0),
            "conversations_closed": r.get("closed", 0), "sales": r.get("sales", 0),
            "first_response_median_min": round(median(times) / 60, 1) if times else None,
            "sla_pct": _pct(r.get("within_sla", 0), r.get("first_responses", 0)),
        })
    within = sum(1 for _a, s in fr if s <= sla * 60)
    return {"sla_minutes": sla, "sla_pct": _pct(within, len(fr)), "handoffs_answered": len(fr),
            "handoffs": handoffs, "agents": rows}


# --- Facturación ----------------------------------------------------------------
@router.get("/reports/billing")
async def billing(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    """Mensajes cobrables según la información de precios que Meta envía en los webhooks de estado."""
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select day, pricing_category as cat, messages, billable from reporting.daily_billing
        where organization_id = :o and day between :a and :b""", o=org, a=start, b=end)
    cats: dict[str, Counter] = defaultdict(Counter)
    by_day: dict[date, Counter] = defaultdict(Counter)
    for r in rows:
        cats[r["cat"]]["total"] += r["messages"]
        cats[r["cat"]]["billable"] += r["billable"]
        if r["billable"]:
            by_day[r["day"]][r["cat"]] += r["billable"]
    return {
        "categories": {k: dict(v) for k, v in cats.items()},
        "billable_total": sum(v["billable"] for v in cats.values()),
        "series": [{"day": d.isoformat(), **dict(by_day.get(d, {}))} for d in _days(start, end)],
        "note": "Cantidades según Meta; el valor en dinero depende de la tarifa por país y categoría.",
    }


# --- IA (salud de los Cortex) ---------------------------------------------------
@router.get("/reports/ai")
async def ai_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    """Llamadas a LLMs: volumen, errores, failover, latencias, tokens y costo por conexión y propósito."""
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select d.day, d.connection_id, d.purpose, d.calls, d.errors, d.slow, d.fallbacks, d.latency_p50_ms,
               d.latency_p95_ms, d.input_tokens, d.output_tokens, d.cost_usd,
               c.name, c.provider, c.model, h.state
        from reporting.daily_ai d
        left join public.ai_connections c on c.id = d.connection_id
        left join public.ai_connection_health h on h.connection_id = d.connection_id
        where d.organization_id = :o and d.day between :a and :b""", o=org, a=start, b=end)
    groups: dict[tuple, dict] = {}
    series: dict[date, Counter] = defaultdict(Counter)
    for r in rows:
        key = (r["connection_id"], r["purpose"])
        g = groups.setdefault(key, {
            "connection_id": r["connection_id"] or None, "name": r["name"] or "Sin conexión", "provider": r["provider"],
            "model": r["model"], "purpose": r["purpose"], "state": r["state"] or "closed", "calls": 0, "errors": 0,
            "slow": 0, "fallbacks": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "latency_p50_ms": None, "latency_p95_ms": None})
        for k in ("calls", "errors", "slow", "fallbacks", "input_tokens", "output_tokens"):
            g[k] += r[k] or 0
        g["cost_usd"] += float(r["cost_usd"] or 0)
        # Latencias: peor día del rango (conservador; las exactas están en ai_calls)
        for k in ("latency_p50_ms", "latency_p95_ms"):
            if r[k] is not None:
                g[k] = max(g[k] or 0, r[k])
        s = series[r["day"]]
        s["calls"] += r["calls"]
        s["errors"] += r["errors"]
        s["fallbacks"] += r["fallbacks"]
    items = sorted(groups.values(), key=lambda g: -g["calls"])
    for g in items:
        g["error_pct"] = _pct(g["errors"] + g["slow"], g["calls"])
        g["cost_usd"] = round(g["cost_usd"], 6)
    totals = Counter()
    for g in items:
        for k in ("calls", "errors", "slow", "fallbacks", "input_tokens", "output_tokens"):
            totals[k] += g[k]
    return {
        "totals": {**dict(totals), "cost_usd": round(sum(g["cost_usd"] for g in items), 6),
                   "error_pct": _pct(totals["errors"] + totals["slow"], totals["calls"])},
        "by_connection": items,
        "series": [{"day": d.isoformat(), **dict(series.get(d, {}))} for d in _days(start, end)],
    }


# --- Análisis de flujos ---------------------------------------------------------
BLOCK_LABELS = {b["type"]: b["label"] for b in BLOCKS}


def _walk_blocks(definition: dict) -> list[dict]:
    """Bloques del flujo en orden de lectura (script → bloques → ramas), con profundidad y rama."""
    out: list[dict] = []

    def walk(blocks, script: str, depth: int, lane: str | None) -> None:
        for b in blocks or []:
            if not isinstance(b, dict) or not b.get("id"):
                continue
            options = [str(o if isinstance(o, str) else o.get("title", ""))
                       for o in (b.get("inputs") or {}).get("options") or []]
            out.append({"block_id": b["id"], "block_type": b.get("type"), "script": script, "depth": depth,
                        "lane": lane, "options": options})
            # jsonb no conserva el orden de las claves: ramas en el orden en que se leen en el editor
            branches = b.get("branches") or {}
            order = [*options, "then", "else", "body", "other"]
            for name in sorted(branches, key=lambda n: order.index(n) if n in order else len(order)):
                walk(branches[name], script, depth + 1, name)

    for s in definition.get("scripts") or []:
        walk(s.get("blocks"), s.get("name") or s.get("id") or "", 0, None)
    return out


def _flow_totals(rows: list[dict]) -> dict:
    t = Counter()
    for r in rows:
        for k in ("started", "succeeded", "failed", "cancelled", "waiting", "duration_sum_s", "finished"):
            t[k] += r[k] or 0
    return {"started": t["started"], "succeeded": t["succeeded"], "failed": t["failed"],
            "cancelled": t["cancelled"], "waiting": t["waiting"],
            "completion_pct": _pct(t["succeeded"], t["started"]),
            "avg_duration_s": round(t["duration_sum_s"] / t["finished"]) if t["finished"] else None}


@router.get("/reports/flows")
async def flows_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    """Resumen por flujo: ejecuciones, finalización, fallos y duración (reporting.daily_flows)."""
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select d.* from reporting.daily_flows d
        where d.organization_id = :o and d.day between :a and :b""", o=org, a=start, b=end)
    flows = (await session.scalars(select(Flow).where(Flow.organization_id == org, Flow.status != "archived")
                                   .order_by(Flow.name))).unique().all()
    by_flow: dict[int, list[dict]] = defaultdict(list)
    series: dict[date, Counter] = defaultdict(Counter)
    for r in rows:
        by_flow[r["flow_id"]].append(r)
        series[r["day"]]["started"] += r["started"]
        series[r["day"]]["succeeded"] += r["succeeded"]
        series[r["day"]]["failed"] += r["failed"]
    items = [{"flow_id": f.id, "name": f.name, "status": f.status, "trigger_type": f.trigger_type,
              **_flow_totals(by_flow.get(f.id, []))} for f in flows]
    items.sort(key=lambda i: -i["started"])
    return {"start": start, "end": end, "totals": _flow_totals(rows), "flows": items,
            "series": [{"day": d.isoformat(), **dict(series.get(d, {}))} for d in _days(start, end)]}


@router.get("/reports/flows/{flow_id}")
async def flow_funnel(flow_id: int, start: date | None = None, end: date | None = None,
                      agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Embudo de un flujo: por bloque, cuántas ejecuciones llegan, abandonan, fallan o esperan, y las opciones
    elegidas. El orden es el de la versión vigente; los bloques que ya no existen van al final."""
    org = agent.organization_id
    flow = await session.get(Flow, flow_id)
    if not flow or flow.organization_id != org:
        raise HTTPException(404, "Flujo no encontrado")
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    version = flow.current_version or await session.scalar(
        select(FlowVersion).where(FlowVersion.flow_id == flow.id).order_by(FlowVersion.version.desc()).limit(1))
    layout = _walk_blocks(version.definition if version else {})

    runs = await _rows(session, """
        select * from reporting.daily_flows where organization_id = :o and flow_id = :f and day between :a and :b""",
                       o=org, f=flow.id, a=start, b=end)
    blocks = await _rows(session, """
        select block_id, max(block_type) as block_type, sum(runs)::int as runs, sum(executions)::int as executions,
               sum(errors)::int as errors, sum(waits)::int as waits, sum(exits)::int as exits,
               sum(stuck)::int as stuck, max(latency_p50_ms) as latency_p50_ms, max(latency_p95_ms) as latency_p95_ms
        from reporting.daily_flow_blocks
        where organization_id = :o and flow_id = :f and day between :a and :b
        group by block_id""", o=org, f=flow.id, a=start, b=end)
    choices = await _rows(session, """
        select block_id, choice, sum(runs)::int as runs from reporting.daily_flow_choices
        where organization_id = :o and flow_id = :f and day between :a and :b
        group by block_id, choice order by runs desc""", o=org, f=flow.id, a=start, b=end)

    totals = _flow_totals(runs)
    stats = {b["block_id"]: b for b in blocks}
    by_block: dict[str, list[dict]] = defaultdict(list)
    for c in choices:
        by_block[c["block_id"]].append({"choice": c["choice"], "runs": c["runs"]})

    def row(meta: dict, removed: bool = False) -> dict:
        st = stats.get(meta["block_id"], {})
        reached = st.get("runs") or 0
        opts = by_block.get(meta["block_id"], [])
        chosen = sum(o["runs"] for o in opts)
        return {**meta, "label": BLOCK_LABELS.get(meta["block_type"], meta["block_type"]), "removed": removed,
                "runs": reached, "reach_pct": _pct(reached, totals["started"]),
                "executions": st.get("executions") or 0, "errors": st.get("errors") or 0,
                "waits": st.get("waits") or 0, "exits": st.get("exits") or 0, "stuck": st.get("stuck") or 0,
                "drop_pct": _pct((st.get("exits") or 0) + (st.get("stuck") or 0), reached),
                "latency_p50_ms": st.get("latency_p50_ms"), "latency_p95_ms": st.get("latency_p95_ms"),
                "choices": [{**o, "pct": _pct(o["runs"], chosen)} for o in opts]}

    known = {m["block_id"] for m in layout}
    out = [row(m) for m in layout]
    out += [row({"block_id": b["block_id"], "block_type": b["block_type"], "script": None, "depth": 0,
                 "lane": None, "options": []}, removed=True)
            for b in blocks if b["block_id"] not in known]
    worst = max((b for b in out if b["runs"]), key=lambda b: b["exits"] + b["stuck"], default=None)
    return {"flow": {"id": flow.id, "name": flow.name, "status": flow.status,
                     "version": version.version if version else None},
            "start": start, "end": end, "totals": totals, "blocks": out,
            "top_dropoff": worst["block_id"] if worst and (worst["exits"] + worst["stuck"]) else None,
            "series": [{"day": r["day"].isoformat(), "started": r["started"], "succeeded": r["succeeded"],
                        "failed": r["failed"]} for r in sorted(runs, key=lambda r: r["day"])]}


# --- KPIs de servicio, reporte Login y enlaces (§18.2) ----------------------------
SERVICE_KEYS = ("cases", "unique_contacts", "returning_cases", "bot_only", "handoffs", "attended", "not_attended",
                "abandoned", "reassigned", "closed", "aht_sum_s", "aht_count", "wait_sum_s", "wait_count")


def _service_kpis(t: Counter) -> dict:
    """Totales + indicadores derivados (seguros ante división por cero)."""
    out = {k: int(t.get(k, 0)) for k in SERVICE_KEYS if not k.endswith(("_sum_s", "_count"))}
    out["aht_s"] = round(t["aht_sum_s"] / t["aht_count"]) if t.get("aht_count") else None
    out["asa_s"] = round(t["wait_sum_s"] / t["wait_count"]) if t.get("wait_count") else None
    out["attention_rate_pct"] = _pct(t.get("attended", 0), t.get("handoffs", 0))
    out["abandonment_rate_pct"] = _pct(t.get("abandoned", 0), t.get("handoffs", 0))
    out["bot_containment_pct"] = _pct(t.get("bot_only", 0), t.get("closed", 0))
    return out


@router.get("/reports/service")
async def service_report(start: date | None = None, end: date | None = None, group_id: int | None = None,
                         agent_id: int | None = None, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    """AHT, ASA, tasa de atención y de abandono, contención del bot, clientes únicos vs casos (daily_service)."""
    org = agent.organization_id
    lo, hi, _tz, start, end = await _range(session, org, start, end)
    scope = await scope_for(session, agent)
    rows = await _rows(session, """
        select * from reporting.daily_service where organization_id = :o and day between :a and :b""",
        o=org, a=start, b=end)
    if not scope.unrestricted:
        rows = [r for r in rows if r["group_id"] in scope.group_ids or r["agent_id"] in scope.member_ids
                or r["agent_id"] == scope.agent_id]
    if group_id is not None:
        rows = [r for r in rows if r["group_id"] == group_id]
    if agent_id is not None:
        rows = [r for r in rows if r["agent_id"] == agent_id]
    totals: Counter = Counter()
    by_group: dict[int, Counter] = defaultdict(Counter)
    by_agent: dict[int, Counter] = defaultdict(Counter)
    by_day: dict[date, Counter] = defaultdict(Counter)
    for r in rows:
        for k in SERVICE_KEYS:
            v = int(r[k] or 0)
            totals[k] += v
            by_group[r["group_id"]][k] += v
            by_agent[r["agent_id"]][k] += v
            by_day[r["day"]][k] += v
    # Clientes únicos: exactos desde las conversaciones (no se pueden sumar entre filas del rollup)
    cv = Conversation
    conds = [cv.organization_id == org, cv.created_at >= lo, cv.created_at < hi]
    clause = conversation_clause(scope)
    if clause is not None:
        conds.append(clause)
    if group_id is not None:
        conds.append(func.coalesce(cv.group_id, 0) == group_id)
    if agent_id is not None:
        conds.append(func.coalesce(cv.assigned_agent_id, 0) == agent_id)
    totals["unique_contacts"] = await session.scalar(
        select(func.count(func.distinct(cv.contact_id))).where(*conds)) or 0
    for key_col, bucket in ((func.coalesce(cv.group_id, 0), by_group), (func.coalesce(cv.assigned_agent_id, 0), by_agent)):
        for k, n in (await session.execute(select(key_col, func.count(func.distinct(cv.contact_id)))
                                           .where(*conds).group_by(key_col))).all():
            if k in bucket:
                bucket[k]["unique_contacts"] = n
    names_g = {g.id: g.name for g in (await session.scalars(select(Group).where(Group.organization_id == org))).all()}
    names_a = {a.id: a.name for a in (await session.scalars(select(Agent).where(Agent.organization_id == org))).all()}
    return {
        "start": start, "end": end, "totals": _service_kpis(totals),
        "by_group": sorted(({"group_id": g or None, "name": names_g.get(g, "Sin grupo"), **_service_kpis(t)}
                            for g, t in by_group.items()), key=lambda x: -x["cases"]),
        "by_agent": sorted(({"agent_id": a or None, "name": names_a.get(a, "Sin asesor"), **_service_kpis(t)}
                            for a, t in by_agent.items()), key=lambda x: -x["cases"]),
        "series": [{"day": d.isoformat(), **_service_kpis(by_day.get(d, Counter()))} for d in _days(start, end)],
    }


@router.get("/reports/login")
async def login_report(start: date | None = None, end: date | None = None, agent_id: int | None = None,
                       agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Tiempo por estado de cada asesor (daily_agent_status), tiempo laborado y primer ingreso / última salida."""
    org = agent.organization_id
    lo, hi, tz, start, end = await _range(session, org, start, end)
    scope = await scope_for(session, agent)
    a_stmt = select(Agent).where(Agent.organization_id == org).order_by(Agent.name)
    clause = agent_clause(scope)
    agents = (await session.scalars(a_stmt if clause is None else a_stmt.where(clause))).all()
    if agent_id is not None:
        agents = [a for a in agents if a.id == agent_id]
    ids = [a.id for a in agents] or [0]
    statuses = (await session.scalars(select(AgentStatus).where(AgentStatus.organization_id == org)
                                      .order_by(AgentStatus.position))).all()
    working = {st.key for st in statuses if st.counts_as_working}
    by_agent: dict[int, Counter] = defaultdict(Counter)
    for r in await _rows(session, """
            select agent_id, status_key, sum(seconds)::bigint as seconds from reporting.daily_agent_status
            where organization_id = :o and day between :a and :b and agent_id = any(:ids)
            group by agent_id, status_key""", o=org, a=start, b=end, ids=ids):
        by_agent[r["agent_id"]][r["status_key"]] += int(r["seconds"])
    sessions: dict[int, dict[date, dict]] = defaultdict(dict)
    for s_row in (await session.scalars(select(AgentSession).where(
            AgentSession.organization_id == org, AgentSession.agent_id.in_(ids),
            AgentSession.started_at < hi, func.coalesce(AgentSession.ended_at, AgentSession.last_seen_at) >= lo)
            .order_by(AgentSession.started_at))).all():
        day = s_row.started_at.astimezone(tz).date()
        end_at = s_row.ended_at or s_row.last_seen_at
        d = sessions[s_row.agent_id].setdefault(day, {"day": day.isoformat(), "first_login": s_row.started_at,
                                                      "last_logout": end_at, "sessions": 0})
        d["first_login"] = min(d["first_login"], s_row.started_at)
        d["last_logout"] = max(d["last_logout"], end_at) if end_at else d["last_logout"]
        d["sessions"] += 1
    return {
        "start": start, "end": end,
        "statuses": [{"key": st.key, "name": st.name, "color": st.color, "counts_as_working": st.counts_as_working}
                     for st in statuses],
        "agents": [{"agent_id": a.id, "name": a.name, "employee_code": getattr(a, "employee_code", None),
                    "worked_s": sum(v for k, v in by_agent[a.id].items() if k in working),
                    "total_s": sum(by_agent[a.id].values()), "by_status": dict(by_agent[a.id]),
                    "sessions": list(sessions[a.id].values())} for a in agents],
    }


URL_RE = r"https?://[^\s<>\"')]+"


class _ConvRef:
    """Lo mínimo que app.scope.conversation_visible necesita de una conversación."""

    def __init__(self, group_id, assigned_agent_id):
        self.group_id, self.assigned_agent_id = group_id, assigned_agent_id


@router.get("/reports/links")
async def links_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    """Enlaces enviados por clientes, asesores y bot (Atom: reporte "Links")."""
    org = agent.organization_id
    lo, hi, _tz, start, end = await _range(session, org, start, end)
    scope = await scope_for(session, agent)
    sql = """
        select m.sender_type, (regexp_matches(m.text, :re, 'g'))[1] as url, c.group_id, c.assigned_agent_id
        from public.messages m join public.conversations c on c.id = m.conversation_id
        where m.organization_id = :o and m.created_at >= :lo and m.created_at < :hi and m.text ~ 'https?://'
        limit 20000"""
    found = await _rows(session, sql, o=org, lo=lo, hi=hi, re=URL_RE)
    if not scope.unrestricted:
        found = [r for r in found if conversation_visible(scope, _ConvRef(r["group_id"], r["assigned_agent_id"]))]
    rows = [(r["sender_type"], r["url"]) for r in found]
    who = {"contact": "client", "agent": "agent", "bot": "bot", "flow": "bot", "campaign": "campaign"}
    urls: dict[str, Counter] = defaultdict(Counter)
    domains: dict[str, Counter] = defaultdict(Counter)
    for sender, match in rows:
        url = match.rstrip(".,;:")
        kind = who.get(sender, "other")
        urls[url][kind] += 1
        domains[urlparse(url).netloc.lower().removeprefix("www.")][kind] += 1

    def rows_of(d: dict[str, Counter], key: str) -> list[dict]:
        out = [{key: k, "total": sum(v.values()), **dict(v)} for k, v in d.items()]
        return sorted(out, key=lambda x: -x["total"])[:200]

    return {"start": start, "end": end, "total": len(rows), "domains": rows_of(domains, "domain"),
            "urls": rows_of(urls, "url")}


# --- Exportación ----------------------------------------------------------------
@router.get("/reports/conversations.csv")
async def export_conversations(start: date | None = None, end: date | None = None,
                               agent: Agent = Depends(require_permission("exports.conversations")),
                               session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, _s, _e = await _range(session, org, start, end)
    c_stmt = select(Conversation).where(
        Conversation.organization_id == org, Conversation.last_message_at >= lo, Conversation.created_at < hi)
    clause = conversation_clause(await scope_for(session, agent))
    convs = (await session.scalars((c_stmt if clause is None else c_stmt.where(clause))
                                   .order_by(Conversation.id))).unique().all()
    fields = list((await fields_by_key(session, org)).values())

    def fmt(dt):
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else ""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "telefono", "nombre", "etapa", "estado", "asesor", "grupo", "tipificacion", "tipificacion_ia",
                "etiquetas", "sentimiento_ia", "resumen_ia", "origen_anuncio", "creada", "transferida",
                "motivo_transferencia", "primera_respuesta_min", "cerrada", "mensajes", *[f.key for f in fields]])
    for c in convs:
        values = custom_values(c.contact)
        first = (round((c.first_response_at - c.handoff_at).total_seconds() / 60, 1)
                 if c.first_response_at and c.handoff_at else "")
        w.writerow([
            c.id, c.contact.wa_id, c.contact.name or "", c.contact.stage, c.status,
            c.assigned_agent.name if c.assigned_agent else "", c.group.name if c.group else "",
            c.typification.name if c.typification else "", c.ai_typification.name if c.ai_typification else "",
            "|".join(sorted(link.tag.name for link in c.tag_links)), c.ai_sentiment or "", c.ai_summary or "",
            c.ad_headline or "", fmt(c.created_at), fmt(c.handoff_at), c.handoff_reason or "", first,
            fmt(c.closed_at), c.message_count, *[values.get(f.key, "") for f in fields],
        ])
    buf.seek(0)
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=conversaciones_{lo.date()}_{hi.date()}.csv"})
