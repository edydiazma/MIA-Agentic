"""Campana de notificaciones, preferencias, notas internas con @menciones y línea de tiempo de la conversación."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.db import get_session
from app.models import (
    Agent,
    AutomationRun,
    Automation,
    ConversationEvent,
    FlowRun,
    Flow,
    Group,
    Message,
    Notification,
    Tag,
    Typification,
    utcnow,
)
from app.notifications import TYPES, mentioned_agents, notify, out, prefs_of
from app.service import commit_and_broadcast, get_conversation, message_out, record_message

router = APIRouter(prefix="/api", tags=["notifications"])


# --- Notificaciones --------------------------------------------------------------------------------------------
@router.get("/notifications")
async def list_notifications(unread: bool = False, limit: int = Query(default=30, le=100), cursor: int | None = None,
                             agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    stmt = select(Notification).where(Notification.agent_id == agent.id,
                                      Notification.organization_id == agent.organization_id)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    if cursor:
        stmt = stmt.where(Notification.id < cursor)
    rows = (await session.scalars(stmt.order_by(Notification.id.desc()).limit(limit + 1))).all()
    items = [out(n) for n in rows[:limit]]
    return {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            "unread": await _unread(session, agent)}


async def _unread(session: AsyncSession, agent: Agent) -> int:
    from sqlalchemy import func

    return await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.agent_id == agent.id, Notification.organization_id == agent.organization_id,
        Notification.read_at.is_(None))) or 0


@router.get("/notifications/unread-count")
async def unread_count(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return {"unread": await _unread(session, agent)}


class ReadIn(BaseModel):
    ids: list[int] = []
    all: bool = False


@router.post("/notifications/read")
async def mark_read(body: ReadIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if not body.all and not body.ids:
        raise HTTPException(422, "Indica ids o all")
    stmt = update(Notification).where(Notification.agent_id == agent.id,
                                      Notification.organization_id == agent.organization_id,
                                      Notification.read_at.is_(None))
    if not body.all:
        stmt = stmt.where(Notification.id.in_(body.ids))
    await session.execute(stmt.values(read_at=utcnow()))
    await session.commit()
    return {"unread": await _unread(session, agent)}


class PrefsIn(BaseModel):
    sound: bool | None = None
    desktop: bool | None = None
    types: dict[str, bool] | None = None


@router.get("/me/notification-prefs")
async def get_prefs(agent: Agent = Depends(current_agent)):
    return prefs_of(agent)


@router.put("/me/notification-prefs")
async def put_prefs(body: PrefsIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    me = await session.get(Agent, agent.id)
    current = prefs_of(me)
    if body.types:
        unknown = set(body.types) - set(TYPES)
        if unknown:
            raise HTTPException(422, f"Tipos desconocidos: {', '.join(sorted(unknown))}")
        current["types"] = {**current["types"], **body.types}
    for k in ("sound", "desktop"):
        if getattr(body, k) is not None:
            current[k] = getattr(body, k)
    me.notification_prefs = current
    await session.commit()
    return current


# --- Notas internas con @menciones -----------------------------------------------------------------------------
class NoteIn(BaseModel):
    text: str


@router.post("/conversations/{conv_id}/notes")
async def add_note(conv_id: int, body: NoteIn, agent: Agent = Depends(current_agent),
                   session: AsyncSession = Depends(get_session)):
    """Nota visible solo para el equipo (no se envía al cliente). @nombre avisa a ese asesor."""
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "La nota está vacía")
    conv = await get_conversation(session, conv_id, agent.organization_id)
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")
    msg = await record_message(session, conv, Message(direction="out", sender_type="system", type="text",
                                                      text=f"📝 Nota de {agent.name}: {text[:4000]}", status="sent"))
    await commit_and_broadcast(session, conv)
    mentioned = [a for a in await mentioned_agents(session, agent.organization_id, text) if a.id != agent.id]
    who = conv.contact.name or conv.contact.wa_id or "un cliente"
    for a in mentioned:
        await notify(session, a.id, "mention", f"{agent.name} te mencionó", f"{who}: {text[:140]}",
                     f"/conversaciones?id={conv.id}", {"conversation_id": conv.id, "message_id": msg.id})
    return {"message": message_out(msg), "mentioned": [{"id": a.id, "name": a.name} for a in mentioned]}


# --- Línea de tiempo ------------------------------------------------------------------------------------------
LABELS = {
    "created": "Conversación iniciada", "reopened": "Conversación reabierta", "handoff": "Transferida a un asesor",
    "released": "Devuelta al bot", "assigned": "Asignada", "unassigned": "Sin asesor asignado",
    "routed": "Enrutada a grupo", "first_agent_response": "Primera respuesta del asesor", "tagged": "Etiqueta agregada",
    "untagged": "Etiqueta quitada", "typified": "Tipificada", "classified": "Clasificada por IA", "closed": "Cerrada",
    "flow_started": "Flujo iniciado", "flow_finished": "Flujo terminado", "campaign_sent": "Campaña enviada",
    "deal_won": "Negocio ganado", "deal_lost": "Negocio perdido", "call_started": "Llamada iniciada",
    "call_ended": "Llamada terminada", "recovery_sent": "Intento de recuperación enviado",
    "inactivity_closed": "Cerrada por inactividad", "security_flagged": "Marcada por seguridad",
    "stage_changed": "Cambio de etapa", "source_rule_applied": "Regla por fuente aplicada",
    "webhook_triggered": "Webhook entrante",
}
ICONS = {"created": "🟢", "reopened": "🔄", "handoff": "🙋", "released": "🤖", "assigned": "👤", "unassigned": "👤",
         "routed": "🧭", "first_agent_response": "💬", "tagged": "🏷️", "untagged": "🏷️", "typified": "✅",
         "classified": "🧠", "closed": "🔒", "flow_started": "🔀", "flow_finished": "🔀", "campaign_sent": "📣",
         "deal_won": "🏆", "deal_lost": "✖️", "call_started": "📞", "call_ended": "📞", "recovery_sent": "⏰",
         "inactivity_closed": "💤", "security_flagged": "🛡️", "stage_changed": "📈", "source_rule_applied": "🎯",
         "webhook_triggered": "🔗", "sla": "⏱️"}


@router.get("/conversations/{conv_id}/timeline")
async def timeline(conv_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conv = await get_conversation(session, conv_id, agent.organization_id)
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")
    org = agent.organization_id
    events = (await session.scalars(select(ConversationEvent).where(
        ConversationEvent.organization_id == org, ConversationEvent.conversation_id == conv_id)
        .order_by(ConversationEvent.occurred_at, ConversationEvent.id))).all()
    agents = {a.id: a.name for a in (await session.scalars(select(Agent).where(Agent.organization_id == org))).all()}
    groups = {g.id: g.name for g in (await session.scalars(select(Group).where(Group.organization_id == org))).all()}
    typs = {t.id: t.name for t in (await session.scalars(
        select(Typification).where(Typification.organization_id == org))).all()}
    tags = {t.id: t.name for t in (await session.scalars(select(Tag).where(Tag.organization_id == org))).all()}
    runs = {r.id: r for r in (await session.scalars(select(FlowRun).where(
        FlowRun.organization_id == org, FlowRun.conversation_id == conv_id))).all()}
    flows = {f.id: f.name for f in (await session.scalars(select(Flow).where(
        Flow.id.in_({r.flow_id for r in runs.values()} or {0})))).unique().all()}

    items = []
    for e in events:
        p = e.payload or {}
        detail = None
        if e.event_type in ("assigned", "unassigned"):
            frm, to = agents.get(p.get("from")), agents.get(p.get("to") or e.assigned_agent_id)
            detail = f"{frm} → {to}" if frm and to else to or (f"antes {frm}" if frm else None)
        elif e.event_type == "routed":
            detail = " → ".join(x for x in (groups.get(p.get("from")), groups.get(p.get("to") or e.group_id)) if x)
        elif e.event_type == "handoff":
            detail = p.get("reason")
        elif e.event_type == "typified":
            detail = typs.get(p.get("typification_id"))
        elif e.event_type == "classified":
            detail = ", ".join(x for x in (typs.get(p.get("ai_typification_id")), p.get("sentiment")) if x) or None
        elif e.event_type in ("tagged", "untagged"):
            detail = tags.get(p.get("tag_id"))
        elif e.event_type == "first_agent_response" and p.get("seconds") is not None:
            detail = f"{round(int(p['seconds']) / 60, 1)} min"
        elif e.event_type in ("flow_started", "flow_finished"):
            run = runs.get(p.get("run_id"))
            detail = flows.get(p.get("flow_id") or (run.flow_id if run else None)) or p.get("flow")
        elif e.event_type == "closed":
            detail = typs.get(p.get("typification_id"))
        else:
            detail = p.get("reason") or p.get("stage") or p.get("rule") or p.get("name")
        items.append({"at": e.occurred_at, "type": e.event_type, "label": LABELS.get(e.event_type, e.event_type),
                      "icon": ICONS.get(e.event_type, "•"), "detail": detail, "actor_type": e.actor_type,
                      "actor": agents.get(e.actor_agent_id) or {"ai": "IA", "flow": "Flujo", "contact": "Cliente",
                                                                "system": "Sistema"}.get(e.actor_type)})
    # Ejecuciones de flujos sin evento propio y automatizaciones de SLA
    has_flow_events = any(i["type"] == "flow_started" for i in items)
    for r in runs.values():
        if not has_flow_events:
            items.append({"at": r.started_at, "type": "flow_started", "label": LABELS["flow_started"],
                          "icon": ICONS["flow_started"], "detail": flows.get(r.flow_id), "actor_type": "flow",
                          "actor": "Flujo"})
    sla = (await session.execute(select(AutomationRun, Automation.name).join(
        Automation, Automation.id == AutomationRun.automation_id).where(
        AutomationRun.organization_id == org, AutomationRun.conversation_id == conv_id))).all()
    for run, name in sla:
        items.append({"at": run.fired_at, "type": "sla", "label": f"Automatización: {name}", "icon": ICONS["sla"],
                      "detail": run.action, "actor_type": "system", "actor": "Sistema"})
    items.sort(key=lambda i: i["at"])
    return {"conversation_id": conv_id, "items": items}
