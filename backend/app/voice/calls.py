"""Señalización de llamadas de WhatsApp (webhook field "calls") y control desde el panel.

Flujo entrante (USER_INITIATED):
  connect (SDP offer) → se crea la llamada →
    · agente de voz IA (si el canal tiene uno y el plan lo permite): el servidor responde con aiortc
    · si no: se avisa a los asesores conectados ("call.incoming"); el primero que contesta desde el navegador
      genera el SDP answer y el backend lo envía con pre_accept + accept (el audio va navegador ↔ Meta).
  terminate → fin, duración, tarjeta "call" en el chat y resumen con IA.
"""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, set_actor
from app.models import Agent, AgentGroup, Call, CallEvent, CallTurn, Channel, Message, VoiceAgent, utcnow
from app.realtime import hub
from app.secrets_vault import get_secret
from app.voice.meta import CallingClient

log = logging.getLogger(__name__)
RING_TIMEOUT_S = 45
TRANSFER_TIMEOUT_S = 30
ACTIVE = ("ringing", "pre_accepted", "connected", "transferring")
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def calling_client(session: AsyncSession, channel: Channel) -> CallingClient:
    return CallingClient(channel.phone_number_id, await get_secret(session, channel.access_token_secret_id))


def call_out(c: Call) -> dict:
    return {
        "id": c.id, "organization_id": c.organization_id, "channel_id": c.channel_id, "contact_id": c.contact_id,
        "contact_name": c.contact.name if c.contact else None, "wa_id": c.contact.wa_id if c.contact else None,
        "conversation_id": c.conversation_id, "wa_call_id": c.wa_call_id, "direction": c.direction,
        "status": c.status, "handled_by": c.handled_by, "voice_agent_id": c.voice_agent_id, "agent_id": c.agent_id,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "answered_at": c.answered_at.isoformat() if c.answered_at else None,
        "ended_at": c.ended_at.isoformat() if c.ended_at else None, "duration_s": c.duration_s,
        "end_reason": c.end_reason, "has_recording": bool(c.recording_path), "summary": c.summary,
    }


async def log_event(session: AsyncSession, call_id: int, type_: str, payload: dict | None = None) -> None:
    session.add(CallEvent(call_id=call_id, type=type_, payload=payload))


def within_hours(hours: dict | None, tz: str, now: datetime | None = None) -> bool:
    if not hours:
        return True
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz))
    if local.weekday() not in hours.get("days", [0, 1, 2, 3, 4, 5, 6]):
        return False
    hhmm = local.strftime("%H:%M")
    return hours.get("start", "00:00") <= hhmm < hours.get("end", "23:59")


async def eligible_agents(session: AsyncSession, org: int, group_id: int | None) -> list[int]:
    """Asesores conectados y disponibles (del grupo, si se indica) a quienes sonará la llamada."""
    online = hub.online_agent_ids()
    if not online:
        return []
    stmt = select(Agent.id).where(Agent.organization_id == org, Agent.is_active, Agent.availability == "available",
                                  Agent.id.in_(online))
    if group_id:
        stmt = stmt.join(AgentGroup, AgentGroup.agent_id == Agent.id).where(AgentGroup.group_id == group_id)
    return list((await session.scalars(stmt)).all())


async def broadcast_call(call: Call, event: str = "call.updated", extra: dict | None = None) -> None:
    await hub.broadcast(event, {**call_out(call), **(extra or {})}, call.organization_id)


# --- Webhook ----------------------------------------------------------------------
async def handle_calls_webhook(value: dict) -> None:
    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
    profiles = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
    for call in value.get("calls") or []:
        event = call.get("event")
        if event == "connect" and call.get("direction", "USER_INITIATED") == "USER_INITIATED":
            await on_connect(phone_number_id, call, profiles.get(call.get("from")))
        elif event == "terminate":
            await on_terminate(call, value.get("errors"))
        else:
            async with SessionLocal() as session:
                c = await session.scalar(select(Call).where(Call.wa_call_id == call.get("id")))
                if c:
                    await log_event(session, c.id, event or "status", _strip_sdp(call))
                    await session.commit()


def _strip_sdp(call: dict) -> dict:
    data = dict(call)
    if isinstance(data.get("session"), dict):
        data["session"] = {"sdp_type": data["session"].get("sdp_type"), "sdp": "<omitido>"}
    return data


async def on_connect(phone_number_id: str, payload: dict, profile_name: str | None) -> Call | None:
    from app.plans import check_limit, has_feature
    from app.service import get_or_create_contact, get_or_create_conversation, send_text, system_note
    from app.settings_store import get_setting

    sdp_offer = (payload.get("session") or {}).get("sdp")
    async with SessionLocal() as session:
        channel = await session.scalar(select(Channel).where(Channel.phone_number_id == phone_number_id))
        if not channel:
            log.warning("Llamada para un número no registrado: %s", phone_number_id)
            return None
        if await session.scalar(select(Call.id).where(Call.wa_call_id == payload["id"])):
            return None  # Meta reintenta: idempotente
        org = channel.organization_id
        await set_actor(session, "contact")
        contact, _ = await get_or_create_contact(session, org, payload.get("from"), profile_name,
                                                 bsuid=payload.get("from_user_id"))
        conv = await get_or_create_conversation(session, channel, contact)
        call = Call(organization_id=org, channel_id=channel.id, contact_id=contact.id, conversation_id=conv.id,
                    wa_call_id=payload["id"], direction="inbound", status="ringing")
        session.add(call)
        await session.flush()
        # La oferta SDP se guarda en el evento: la necesita quien conteste
        await log_event(session, call.id, "connect", {"sdp_offer": sdp_offer, "timestamp": payload.get("timestamp")})
        await session.commit()
        await session.refresh(call)
        await session.refresh(call, ["contact"])

        tz = (await get_setting(session, "company", org))["timezone"]
        reason = None
        if not channel.calling_enabled:
            reason = "Las llamadas están desactivadas en este número"
        elif not within_hours(channel.calling_hours, tz):
            reason = "Llamada fuera del horario de atención"
        elif not await has_feature(session, org, "voice"):
            reason = "El plan no incluye llamadas"
        elif not (await check_limit(session, org, "voice_minutes_month") or {}).get("allowed", True):
            reason = "Se agotaron los minutos de voz del plan"
        if reason:
            await _reject(session, channel, call, reason)
            await system_note(session, conv, f"📞 Llamada rechazada: {reason}")
            await session.commit()
            if "horario" in reason and channel.calling_hours:
                h = channel.calling_hours
                await send_text(session, conv, f"Recibimos tu llamada. Nuestro horario de llamadas es de "
                                               f"{h.get('start', '08:00')} a {h.get('end', '18:00')}. "
                                               "Escríbenos por aquí y te ayudamos.", sender_type="bot")
            return call

        voice_agent = await session.get(VoiceAgent, channel.voice_agent_id) if channel.voice_agent_id else None
        if voice_agent and voice_agent.enabled:
            call.voice_agent_id = voice_agent.id
            await session.commit()
            from app.voice.agent_runtime import start_voice_agent

            spawn(start_voice_agent(call.id, sdp_offer))
            return call

        targets = await eligible_agents(session, org, None)
        await broadcast_call(call, "call.incoming", {"sdp_offer": sdp_offer, "targets": targets, "mode": "answer"})
        spawn(_ring_timeout(call.id))
        return call


async def _reject(session: AsyncSession, channel: Channel, call: Call, reason: str, status: str = "rejected") -> None:
    try:
        await (await calling_client(session, channel)).action(call.wa_call_id, "reject")
    except Exception as e:
        log.warning("No se pudo rechazar la llamada %s: %s", call.wa_call_id, e)
    call.status, call.end_reason, call.ended_at = status, reason, utcnow()
    await log_event(session, call.id, "reject", {"reason": reason})
    await session.commit()
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    await broadcast_call(call)


async def _ring_timeout(call_id: int) -> None:
    await asyncio.sleep(RING_TIMEOUT_S)
    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        if call and call.status == "ringing":
            channel = await session.get(Channel, call.channel_id)
            await _reject(session, channel, call, "Nadie contestó", status="missed")


# --- Acciones desde el panel ----------------------------------------------------------
class CallTaken(Exception):
    pass


async def answer_by_agent(session: AsyncSession, call: Call, agent: Agent, sdp_answer: str) -> Call:
    """El primer asesor que contesta se queda con la llamada (actualización atómica)."""
    won = await session.execute(
        update(Call).where(Call.id == call.id, Call.status == "ringing")
        .values(status="connected", handled_by="agent", agent_id=agent.id, answered_at=utcnow())
        .returning(Call.id))
    if not won.first():
        await session.rollback()
        raise CallTaken()
    await session.commit()
    await session.refresh(call)
    channel = await session.get(Channel, call.channel_id)
    client = await calling_client(session, channel)
    try:
        await client.action(call.wa_call_id, "pre_accept", sdp_answer)
        await client.action(call.wa_call_id, "accept", sdp_answer, callback_data=f"call:{call.id}")
        await log_event(session, call.id, "accept", {"agent_id": agent.id})
    except Exception as e:
        call.status, call.end_reason, call.ended_at = "failed", f"Meta rechazó la respuesta: {e}"[:500], utcnow()
        await log_event(session, call.id, "error", {"error": str(e)[:500]})
    await session.commit()
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    await broadcast_call(call, "call.taken")
    await broadcast_call(call)
    return call


async def reject_call(session: AsyncSession, call: Call, reason: str = "Rechazada por el asesor") -> Call:
    channel = await session.get(Channel, call.channel_id)
    await _reject(session, channel, call, reason)
    return call


async def hangup(session: AsyncSession, call: Call, reason: str = "Colgada desde el panel") -> Call:
    channel = await session.get(Channel, call.channel_id)
    try:
        await (await calling_client(session, channel)).action(call.wa_call_id, "terminate")
    except Exception as e:
        log.warning("terminate falló para %s: %s", call.wa_call_id, e)
    from app.voice.session import registry

    await registry.stop(call.id)
    if call.status in ACTIVE:
        await finish(session, call, "ended", reason)
    return call


# --- Fin de la llamada -------------------------------------------------------------
async def on_terminate(payload: dict, errors: list | None) -> None:
    async with SessionLocal() as session:
        call = await session.scalar(select(Call).where(Call.wa_call_id == payload.get("id")))
        if not call:
            return
        await log_event(session, call.id, "terminate", {k: v for k, v in payload.items() if k != "session"})
        from app.voice.session import registry

        await registry.stop(call.id)
        status_values = payload.get("status") or []
        status_values = status_values if isinstance(status_values, list) else [status_values]
        if call.status not in ACTIVE:
            await session.commit()
            return
        if call.answered_at is None:
            status, reason = "missed", "El cliente colgó antes de que contestaran"
        elif "FAILED" in status_values:
            status, reason = "failed", "; ".join(e.get("message", "") for e in errors or []) or "Falla de la llamada"
        else:
            status, reason = "ended", "Finalizada"
        await finish(session, call, status, reason)


async def finish(session: AsyncSession, call: Call, status: str, reason: str) -> None:
    """Cierra la llamada, deja la tarjeta en el chat y pide el resumen a la IA."""
    from app.models import Conversation
    from app.service import record_message

    call.status, call.end_reason, call.ended_at = status, reason, call.ended_at or utcnow()
    await session.commit()
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    await broadcast_call(call)
    if call.conversation_id:
        conv = await session.get(Conversation, call.conversation_id)
        who = {"voice_agent": "el agente de voz", "agent": "un asesor"}.get(call.handled_by or "", "nadie")
        if status in ("missed", "rejected"):
            text = f"📞 Llamada perdida ({reason.lower()})"
        else:
            mins, secs = divmod(call.duration_s or 0, 60)
            text = f"📞 Llamada atendida por {who} · {mins}:{secs:02d}"
        await record_message(session, conv, Message(direction="out", sender_type="system", type="call", text=text,
                                                    status="sent", metadata_={"call_id": call.id}))
    if call.answered_at:
        spawn(summarize(call.id))


async def summarize(call_id: int) -> None:
    """Transcripción → resumen con el Cortex; queda en calls.summary y como nota en la conversación."""
    from app.ai.router import json_call
    from app.models import Conversation
    from app.service import system_note

    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        turns = (await session.scalars(select(CallTurn).where(CallTurn.call_id == call_id).order_by(CallTurn.id))).all()
        if not call or not turns:
            return
        who = {"contact": "Cliente", "voice_agent": "Agente de voz", "agent": "Asesor"}
        transcript = "\n".join(f"{who.get(t.speaker, t.speaker)}: {t.text}" for t in turns)
        call.transcript = transcript[:50000]
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                  "additionalProperties": False}
        try:
            data, _ = await json_call(call.organization_id, None, "classification",
                                      "Resume la llamada en 2 a 4 frases: motivo, lo acordado y próximos pasos.",
                                      transcript, schema, conversation_id=call.conversation_id)
            call.summary = data["summary"][:2000]
        except Exception as e:
            log.warning("No se pudo resumir la llamada %s: %s", call_id, e)
        await session.commit()
        if call.summary and call.conversation_id:
            conv = await session.get(Conversation, call.conversation_id)
            await system_note(session, conv, f"📝 Resumen de la llamada: {call.summary}")
            await session.commit()
        await session.refresh(call)
        await session.refresh(call, ["contact"])
        await broadcast_call(call)


async def add_turn(call_id: int, speaker: str, text: str, start_ms: int | None = None,
                   end_ms: int | None = None) -> None:
    if not text.strip():
        return
    async with SessionLocal() as session:
        session.add(CallTurn(call_id=call_id, speaker=speaker, text=text.strip(), start_ms=start_ms, end_ms=end_ms))
        await session.commit()
        org = await session.scalar(select(Call.organization_id).where(Call.id == call_id))
    await hub.broadcast("call.turn", {"call_id": call_id, "speaker": speaker, "text": text.strip()}, org)


async def request_transfer(call_id: int) -> bool:
    """El agente de voz pide pasar a un humano: suena en los asesores del grupo de transferencia.
    Devuelve True si un asesor tomó la llamada dentro de TRANSFER_TIMEOUT_S."""
    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        if not call or call.status != "connected":
            return False
        va = await session.get(VoiceAgent, call.voice_agent_id) if call.voice_agent_id else None
        targets = await eligible_agents(session, call.organization_id, va.transfer_group_id if va else None)
        if not targets:
            return False
        call.status = "transferring"
        await log_event(session, call.id, "transfer", {"targets": targets})
        await session.commit()
        await session.refresh(call)
        await session.refresh(call, ["contact"])
        await broadcast_call(call, "call.incoming", {"targets": targets, "mode": "bridge"})
    for _ in range(TRANSFER_TIMEOUT_S * 2):
        await asyncio.sleep(0.5)
        async with SessionLocal() as session:
            call = await session.get(Call, call_id)
            if call.handled_by == "agent":
                return True
            if call.status not in ACTIVE:
                return False
    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        if call.status == "transferring":
            call.status = "connected"
            await log_event(session, call.id, "transfer_timeout")
            await session.commit()
            await session.refresh(call)
            await session.refresh(call, ["contact"])
            await broadcast_call(call, "call.taken")
    return False
