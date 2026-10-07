"""Tareas automatizadas: bienvenida, respuestas y transferencias por palabra clave, horario de atención
(app.business_hours), cierre por inactividad y reglas de SLA con temporizador (§18.1)."""

import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, set_actor
from app.models import Automation, Conversation, Message, utcnow

log = logging.getLogger(__name__)

TYPES = ("welcome", "keyword_reply", "keyword_handoff", "business_hours", "inactivity_close",
         "sla_agent_no_reply", "sla_client_no_reply", "sla_unassigned")
SLA_TYPES = ("sla_agent_no_reply", "sla_client_no_reply", "sla_unassigned")
SLA_ACTIONS = ("send_message", "reassign_in_group", "notify_supervisor", "handoff", "typify", "close")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


def keyword_match(text: str, keywords: list[str], mode: str = "contains") -> str | None:
    t = _norm(text)
    for kw in keywords:
        k = _norm(kw)
        if k and (t == k if mode == "exact" else k in t):
            return kw
    return None


def within_hours(cfg: dict, tz_name: str, now: datetime | None = None) -> bool:
    now = (now or utcnow()).astimezone(ZoneInfo(cfg.get("timezone") or tz_name))
    if now.weekday() not in cfg.get("days", [0, 1, 2, 3, 4]):
        return False
    return cfg.get("start", "00:00") <= now.strftime("%H:%M") < cfg.get("end", "23:59")


async def _rules(session: AsyncSession, org: int, type_: str | None = None) -> list[Automation]:
    stmt = (select(Automation).where(Automation.organization_id == org, Automation.enabled)
            .order_by(Automation.priority, Automation.id))
    if type_:
        stmt = stmt.where(Automation.type == type_)
    return list((await session.scalars(stmt)).all())


async def on_inbound(session: AsyncSession, conv: Conversation, msg: Message, is_new_contact: bool) -> bool:
    """Se evalúa con cada mensaje del cliente mientras lo atiende el bot.
    Devuelve True si una regla ya respondió y el bot de IA no debe contestar."""
    from app.service import handoff, send_text

    if conv.status != "bot":
        return False
    from app import business_hours

    hours = await business_hours.status(session, conv.organization_id, conv.group_id)
    if not hours["open"] and hours["row"] is not None and hours["row"].pause_bot:
        await business_hours.send_out_of_hours(session, conv, hours)  # una vez por periodo cerrado
        return True  # fuera de horario con el bot en pausa: el mensaje queda guardado, nadie responde
    text = msg.text or msg.transcript or ""
    for rule in await _rules(session, conv.organization_id):
        cfg = rule.config or {}
        if rule.type == "welcome" and is_new_contact and cfg.get("message"):
            await send_text(session, conv, cfg["message"], sender_type="bot")
        elif rule.type == "keyword_reply" and text and cfg.get("reply"):
            if keyword_match(text, cfg.get("keywords", []), cfg.get("match", "contains")):
                await send_text(session, conv, cfg["reply"], sender_type="bot")
                return True
        elif rule.type == "keyword_handoff" and text:
            kw = keyword_match(text, cfg.get("keywords", []), cfg.get("match", "contains"))
            if kw:
                if cfg.get("message"):
                    await send_text(session, conv, cfg["message"], sender_type="bot")
                await handoff(session, conv, f"Palabra clave: {kw}", cfg.get("group_id"), actor="automation")
                return True
    return False


async def after_handoff(session: AsyncSession, conv: Conversation) -> None:
    """Compatibilidad: el aviso fuera de horario ahora lo envía service.handoff con app.business_hours."""
    from app import business_hours

    hours = await business_hours.status(session, conv.organization_id, conv.group_id)
    if not hours["open"]:
        await business_hours.send_out_of_hours(session, conv, hours)


async def close_inactive() -> int:
    from app.service import close, send_text, typification_by_name, within_session_window

    closed = 0
    async with SessionLocal() as session:
        rules = (await session.scalars(select(Automation).where(
            Automation.enabled, Automation.type == "inactivity_close"))).all()
        for rule in rules:
            hours = float((rule.config or {}).get("hours") or 0)
            if hours <= 0:
                continue
            cutoff = utcnow() - timedelta(hours=hours)
            convs = (await session.scalars(select(Conversation).where(
                Conversation.organization_id == rule.organization_id, Conversation.status != "closed",
                Conversation.last_message_at < cutoff))).unique().all()
            typ = await typification_by_name(session, rule.organization_id, "Inactividad")
            # Las conversaciones del bot cuyo agente tiene recuperación propia las cierra app.recovery
            from app.models import AIAgent, Channel
            from app.recovery import handled_by_recovery

            with_recovery = set((await session.scalars(select(AIAgent.id).where(
                AIAgent.organization_id == rule.organization_id, AIAgent.recovery_enabled))).all())
            defaults = dict((await session.execute(select(Channel.id, Channel.default_ai_agent_id).where(
                Channel.organization_id == rule.organization_id))).all())
            for conv in convs:
                if with_recovery and handled_by_recovery(conv, with_recovery, defaults.get(conv.channel_id)):
                    continue
                message = (rule.config or {}).get("message")
                if message and within_session_window(conv):
                    await send_text(session, conv, message, sender_type="bot")
                await set_actor(session, "automation")
                await close(session, conv, typ, actor="automation")
                closed += 1
    return closed


async def inactivity_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            n = await close_inactive()
            if n:
                log.info("Cerradas %s conversaciones por inactividad", n)
        except Exception:
            log.exception("Falló el cierre por inactividad")


# --- Reglas de SLA con temporizador -------------------------------------------------------------------------
def validate_sla(config: dict) -> str | None:
    """Mensaje de error o None. config = {minutes, group_ids?, only_business_hours?, actions: [...]}."""
    try:
        minutes = float(config.get("minutes") or 0)
    except (TypeError, ValueError):
        return "Minutos inválidos"
    if minutes <= 0 or minutes > 7 * 24 * 60:
        return "Indica los minutos (mayor que 0)"
    actions = config.get("actions") or []
    if not actions:
        return "Indica al menos una acción"
    for a in actions:
        kind = (a or {}).get("type")
        if kind not in SLA_ACTIONS:
            return f"Acción inválida: {kind}"
        if kind == "send_message" and not str(a.get("text") or "").strip():
            return "La acción «enviar mensaje» necesita el texto"
        if kind == "typify" and not a.get("name"):
            return "La acción «tipificar» necesita la tipificación"
    return None


def _cycle(rule: Automation, conv: Conversation) -> tuple[str, datetime] | None:
    """(clave del ciclo, desde cuándo corre el reloj) o None si la regla no aplica ahora."""
    if rule.type == "sla_agent_no_reply":
        if conv.status != "human" or not conv.assigned_agent_id or conv.last_inbound_at is None:
            return None
        if conv.last_agent_message_at and conv.last_agent_message_at >= conv.last_inbound_at:
            return None  # el asesor ya respondió el último mensaje del cliente
        start = max(conv.last_inbound_at, conv.assigned_at or conv.last_inbound_at)
        return f"in:{conv.last_inbound_at.isoformat()}", start
    if rule.type == "sla_client_no_reply":
        if conv.status != "bot" or conv.last_message_at is None:
            return None
        if conv.last_inbound_at and conv.last_inbound_at >= conv.last_message_at:
            return None  # el último mensaje es del cliente: no está esperando respuesta del cliente
        # el ciclo es el último mensaje del cliente: la acción (p. ej. un mensaje) no reinicia el reloj
        key = f"in:{conv.last_inbound_at.isoformat()}" if conv.last_inbound_at else f"conv:{conv.id}"
        return key, conv.last_message_at
    if rule.type == "sla_unassigned":
        if conv.status != "human" or conv.assigned_agent_id or conv.handoff_at is None:
            return None
        return f"handoff:{conv.handoff_at.isoformat()}", conv.handoff_at
    return None


async def _supervisors(session: AsyncSession, org: int, group_id: int | None) -> list[int]:
    from app.models import Agent, AgentGroup

    ids: list[int] = []
    if group_id:
        ids = list((await session.scalars(select(AgentGroup.agent_id).join(Agent, Agent.id == AgentGroup.agent_id).where(
            AgentGroup.group_id == group_id, AgentGroup.role == "supervisor", Agent.is_active))).all())
    if not ids:
        ids = list((await session.scalars(select(Agent.id).where(
            Agent.organization_id == org, Agent.is_active, Agent.role.in_(("admin", "supervisor"))))).all())
    return ids


async def _run_actions(session: AsyncSession, rule: Automation, conv: Conversation) -> list[dict]:
    from app import routing
    from app.realtime import hub
    from app.service import close, commit_and_broadcast, handoff, send_text, system_note, typification_by_name

    done: list[dict] = []
    label = rule.name or rule.type
    for a in (rule.config or {}).get("actions") or []:
        kind = a.get("type")
        try:
            if kind == "send_message":
                await send_text(session, conv, str(a["text"]), sender_type="bot")
                done.append({"type": kind})
            elif kind == "reassign_in_group" and conv.status == "human":
                previous = conv.assigned_agent_id
                new = await routing.pick_agent(session, conv.organization_id, conv.group_id, conv.contact_id,
                                               exclude={previous} if previous else None)
                if new and new != previous:
                    await set_actor(session, "automation")
                    conv.assigned_agent_id = new
                    await routing.after_assignment(session, conv, new, "transfer")
                    await commit_and_broadcast(session, conv)
                done.append({"type": kind, "from": previous, "to": new})
            elif kind == "notify_supervisor":
                sup = await _supervisors(session, conv.organization_id, conv.group_id)
                for agent_id in sup:
                    await routing._notify(session, agent_id, "sla_breach", f"SLA: {label}",
                                          "Una conversación superó el tiempo configurado",
                                          f"/conversaciones?c={conv.id}", {"conversation_id": conv.id, "rule": rule.id})
                await hub.broadcast("sla.breach", {"conversation_id": conv.id, "rule_id": rule.id, "rule": label,
                                                   "supervisors": sup}, conv.organization_id)
                done.append({"type": kind, "notified": sup})
            elif kind == "handoff" and conv.status == "bot":
                await handoff(session, conv, f"SLA: {label}", a.get("group_id"), actor="automation")
                done.append({"type": kind})
            elif kind == "typify":
                typ = await typification_by_name(session, conv.organization_id, a.get("name"))
                if typ:
                    conv.typification_id = typ.id
                    await commit_and_broadcast(session, conv)
                done.append({"type": kind, "typification": a.get("name")})
            elif kind == "close" and conv.status != "closed":
                typ = await typification_by_name(session, conv.organization_id, a.get("typification"))
                await close(session, conv, typ, actor="automation")
                done.append({"type": kind})
        except Exception as e:  # una acción fallida no detiene las demás
            log.exception("Acción %s de la regla %s falló", kind, rule.id)
            done.append({"type": kind, "error": str(e)[:300]})
    if conv.status != "closed":
        await system_note(session, conv, f"SLA «{label}»: " + ", ".join(d["type"] for d in done))
    return done


async def run_sla(now: datetime | None = None) -> int:
    """Evalúa las reglas de SLA de todas las empresas. Cada regla dispara una vez por ciclo (automation_runs)."""
    from sqlalchemy.dialects.postgresql import insert

    from app import business_hours
    from app.models import AutomationRun

    now = now or utcnow()
    fired = 0
    async with SessionLocal() as session:
        rules = (await session.scalars(select(Automation).where(
            Automation.enabled, Automation.type.in_(SLA_TYPES)).order_by(Automation.priority, Automation.id))).all()
        for rule in rules:
            cfg = rule.config or {}
            minutes = float(cfg.get("minutes") or 0)
            if minutes <= 0:
                continue
            stmt = select(Conversation).where(Conversation.organization_id == rule.organization_id,
                                              Conversation.status != "closed")
            if cfg.get("group_ids"):
                stmt = stmt.where(Conversation.group_id.in_(cfg["group_ids"]))
            if rule.type == "sla_client_no_reply":
                stmt = stmt.where(Conversation.status == "bot")
            else:
                stmt = stmt.where(Conversation.status == "human")
            for conv in (await session.scalars(stmt)).unique().all():
                cyc = _cycle(rule, conv)
                if cyc is None or now - cyc[1] < timedelta(minutes=minutes):
                    continue
                if cfg.get("only_business_hours"):
                    if not (await business_hours.status(session, rule.organization_id, conv.group_id, now))["open"]:
                        continue
                res = await session.execute(insert(AutomationRun).values(
                    organization_id=rule.organization_id, automation_id=rule.id, conversation_id=conv.id,
                    cycle_key=cyc[0], action="pending").on_conflict_do_nothing().returning(AutomationRun.id))
                run_id = res.scalar()
                await session.commit()
                if run_id is None:
                    continue  # ya disparó en este ciclo
                done = await _run_actions(session, rule, conv)
                row = await session.get(AutomationRun, run_id)
                row.action = ",".join(d["type"] for d in done) or "none"
                row.result = {"actions": done}
                await session.commit()
                fired += 1
    return fired


async def sla_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            n = await run_sla()
            if n:
                log.info("Reglas de SLA disparadas: %s", n)
        except Exception:
            log.exception("Falló la evaluación de SLA")
