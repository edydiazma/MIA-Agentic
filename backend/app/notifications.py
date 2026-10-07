"""Centro de notificaciones del panel (campana). docs/data-model.md §18.3.

`notify(...)` es el punto único para avisar a un asesor: guarda la notificación (respeta sus preferencias por
tipo), la entrega en vivo solo a ese asesor (`notification.new`) y, si no está conectado, la envía por Web Push.

Además el hub llama `on_event` una vez por evento publicado: asignaciones / transferencias y mensajes entrantes en
conversaciones asignadas a un asesor desconectado. En ese camino no se reenvía Web Push (lo hace app.push).
"""

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Conversation, ConversationEvent, FollowUp, Notification, utcnow

log = logging.getLogger(__name__)

TYPES = ("assigned", "message", "mention", "followup_due", "callback_due", "sla_breach", "transfer", "call",
         "system", "qa_review", "stage")
# Preferencia de Web Push equivalente (app/push.py); None = sin filtro por preferencia
PUSH_PREF = {"assigned": "assigned", "transfer": "assigned", "message": "message_assigned", "call": "call"}
DEFAULT_PREFS = {"sound": True, "desktop": True,
                 "types": {t: True for t in TYPES}}
_seen: OrderedDict[int, None] = OrderedDict()  # eventos de asignación ya avisados (por réplica)
_tasks: set[asyncio.Task] = set()


def prefs_of(agent: Agent) -> dict:
    stored = agent.notification_prefs or {}
    return {**DEFAULT_PREFS, **{k: v for k, v in stored.items() if k != "types"},
            "types": {**DEFAULT_PREFS["types"], **(stored.get("types") or {})}}


def out(n: Notification) -> dict:
    return {"id": n.id, "type": n.type, "title": n.title, "body": n.body, "link": n.link, "data": n.data or {},
            "read": n.read_at is not None, "read_at": n.read_at, "created_at": n.created_at}


async def notify(session, agent_id: int, type: str, title: str, body: str | None = None, link: str | None = None,
                 data: dict | None = None) -> None:
    """Avisa a un asesor (guarda, entrega en vivo y Web Push si está desconectado). Hace commit."""
    await _notify(session, agent_id, type, title, body, link, data, push=True)


async def _notify(session, agent_id: int, type: str, title: str, body: str | None, link: str | None,
                  data: dict | None, push: bool) -> Notification | None:
    if type not in TYPES:
        raise ValueError(f"Tipo de notificación inválido: {type}")
    agent = await session.get(Agent, agent_id)
    if not agent or not agent.is_active:
        return None
    if type != "system" and not prefs_of(agent)["types"].get(type, True):
        return None
    n = Notification(organization_id=agent.organization_id, agent_id=agent.id, type=type, title=title[:200],
                     body=(body or None) and body[:500], link=link, data=data or {})
    session.add(n)
    await session.commit()
    from app.realtime import hub

    try:
        await hub.send_to_agent(agent.organization_id, agent.id, "notification.new", out(n))
    except Exception:
        log.exception("No se pudo entregar la notificación en vivo")
    if push and agent.id not in hub.online_agent_ids(agent.organization_id):
        try:
            from app import push as webpush

            await webpush.send(session, agent.organization_id, [agent.id], PUSH_PREF.get(type),
                               {"title": title, "body": body or "", "url": link or "/", "tag": f"n-{n.id}"})
        except Exception:
            log.debug("Web Push no disponible", exc_info=True)
    return n


# --- Disparadores desde el hub ---------------------------------------------------------------------------------
async def on_event(event: str, data: dict, organization_id: int | None) -> None:
    if organization_id is None or event not in ("conversation.updated", "message.new"):
        return
    task = asyncio.get_running_loop().create_task(_safe(event, data, organization_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe(event: str, data: dict, org: int) -> None:
    try:
        async with SessionLocal() as session:
            if event == "conversation.updated":
                await _on_assigned(session, data, org)
            else:
                await _on_message(session, data, org)
    except Exception:
        log.exception("Falló la notificación de %s", event)


def _contact_name(data: dict) -> str:
    c = data.get("contact") or {}
    return c.get("name") or c.get("wa_id") or c.get("wa_username") or "Cliente"


async def _on_assigned(session, data: dict, org: int) -> None:
    agent_id = (data.get("assigned_agent") or {}).get("id")
    conv_id = data.get("id")
    if not agent_id or not conv_id:
        return
    ev = (await session.scalars(select(ConversationEvent).where(
        ConversationEvent.organization_id == org, ConversationEvent.conversation_id == conv_id,
        ConversationEvent.event_type == "assigned", ConversationEvent.occurred_at >= utcnow() - timedelta(minutes=2))
        .order_by(ConversationEvent.occurred_at.desc()).limit(1))).first()
    if not ev or ev.assigned_agent_id != agent_id or ev.actor_agent_id == agent_id or ev.id in _seen:
        return  # sin asignación reciente, se la tomó él mismo o ya se avisó
    _seen[ev.id] = None
    while len(_seen) > 5000:
        _seen.popitem(last=False)
    by_agent = ev.actor_type == "agent" and ev.actor_agent_id
    actor = await session.get(Agent, ev.actor_agent_id) if by_agent else None
    kind = "transfer" if actor else "assigned"
    title = f"{actor.name} te transfirió una conversación" if actor else "Te asignaron una conversación"
    await _notify(session, agent_id, kind, title, _contact_name(data), f"/conversaciones?id={conv_id}",
                  {"conversation_id": conv_id}, push=False)


async def _on_message(session, data: dict, org: int) -> None:
    if data.get("direction") != "in" or not data.get("conversation_id"):
        return
    conv = await session.get(Conversation, data["conversation_id"])
    if not conv or conv.organization_id != org or not conv.assigned_agent_id or conv.status == "closed":
        return
    from app.realtime import hub

    if conv.assigned_agent_id in hub.online_agent_ids(org):
        return  # lo ve en el panel (la bandeja marca no leídos)
    # Una sola notificación sin leer por conversación
    pending = await session.scalar(select(Notification.id).where(
        Notification.agent_id == conv.assigned_agent_id, Notification.type == "message",
        Notification.read_at.is_(None), Notification.data["conversation_id"].as_integer() == conv.id).limit(1))
    if pending:
        return
    text = data.get("text") or data.get("transcript") or "Mensaje nuevo"
    await _notify(session, conv.assigned_agent_id, "message", conv.contact.name or "Cliente", text[:140],
                  f"/conversaciones?id={conv.id}", {"conversation_id": conv.id}, push=False)


# --- Menciones en notas internas -----------------------------------------------------------------------------
MENTION_RE = re.compile(r"@([\wÁÉÍÓÚÑáéíóúñ.\-]+(?:\s[\wÁÉÍÓÚÑáéíóúñ.\-]+)?)")


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


async def mentioned_agents(session, org: int, text: str) -> list[Agent]:
    """@nombre, @nombre apellido o @correo (parte local). Sin coincidencia exacta no se avisa a nadie."""
    tokens = {_norm(m.group(1)) for m in MENTION_RE.finditer(text or "")}
    if not tokens:
        return []
    agents = (await session.scalars(select(Agent).where(Agent.organization_id == org, Agent.is_active))).all()
    found = []
    for a in agents:
        names = {_norm(a.name), _norm(a.name.split(" ")[0]), _norm(a.email.split("@")[0])}
        # "@luis martinez" captura dos palabras; "@luis gracias" también: basta que el inicio coincida
        if any(t in names or any(t.startswith(n + " ") for n in names) for t in tokens):
            found.append(a)
    return found


# --- Recordatorios de seguimientos / llamadas ----------------------------------------------------------------
async def remind_due(limit: int = 200) -> int:
    sent = 0
    async with SessionLocal() as session:
        due = (await session.scalars(select(FollowUp).where(
            FollowUp.done_at.is_(None), FollowUp.reminded_at.is_(None), FollowUp.due_at <= utcnow())
            .order_by(FollowUp.due_at).limit(limit))).unique().all()
        for f in due:
            f.reminded_at = utcnow()  # antes de avisar: nunca dos veces aunque falle el envío
            await session.commit()
            kind = "callback_due" if f.kind == "callback" else "followup_due"
            who = (f.contact.name or f.contact.wa_id) if getattr(f, "contact", None) else "un cliente"
            title = f"Llamar a {who}" if kind == "callback_due" else f"Seguimiento pendiente: {who}"
            link = f"/conversaciones?id={f.conversation_id}" if f.conversation_id else f"/clientes?id={f.contact_id}"
            await notify(session, f.agent_id, kind, title, f.note, link,
                         {"followup_id": f.id, "contact_id": f.contact_id})
            sent += 1
    return sent


async def followup_reminder_loop() -> None:
    while True:
        try:
            await remind_due()
        except Exception:
            log.exception("Recordatorios de seguimiento fallaron")
        await asyncio.sleep(60)
