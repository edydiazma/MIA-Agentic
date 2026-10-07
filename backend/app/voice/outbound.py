"""Llamadas de WhatsApp iniciadas por el asesor (docs/data-model.md §19.1).

1. Permiso: WhatsApp exige que el cliente acepte recibir llamadas de la empresa. `request_permission` envía el
   mensaje interactivo `call_permission_request` (límites de Meta por cliente: 1 cada 24 h y 2 cada 7 días; los
   aplicamos antes de llamar a Meta) y deja call_permissions «requested». La respuesta llega como mensaje
   interactivo `call_permission_reply` → `on_permission_reply` (granted/denied, permanente o con vencimiento).
2. Llamada: `start_call` exige un permiso vigente; el navegador del asesor genera la oferta SDP y Meta llama al
   cliente (POST /{phone_number_id}/calls, action connect). El SDP answer llega en el webhook «connect»
   (BUSINESS_INITIATED) y se entrega solo a ese asesor (evento call.answer); RINGING / ACCEPTED / REJECTED y el
   terminate actualizan la llamada como las entrantes. El audio va navegador ↔ Meta (no pasa por el servidor).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Call, CallPermission, Channel, Contact, Conversation, utcnow
from app.realtime import hub

log = logging.getLogger(__name__)
TEMPORARY_DAYS = 7


class OutboundCallError(Exception):
    def __init__(self, message: str, code: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def _address(contact: Contact) -> str:
    to = contact.wa_id or contact.wa_bsuid
    if not to:
        raise OutboundCallError("El cliente no tiene número ni usuario de WhatsApp", "no_address", 422)
    return to


def permission_out(p: CallPermission | None) -> dict:
    if p is None:
        return {"status": "none", "can_call": False, "can_request": True}
    valid = is_valid(p)
    return {"id": p.id, "status": ("granted" if valid else "expired") if p.status == "granted" else p.status,
            "permanent": p.permanent, "can_call": valid,
            "requested_at": p.requested_at, "responded_at": p.responded_at, "expires_at": p.expires_at}


def is_valid(p: CallPermission | None, now: datetime | None = None) -> bool:
    if p is None or p.status != "granted":
        return False
    return p.permanent or (p.expires_at is not None and p.expires_at > (now or utcnow()))


async def latest(session: AsyncSession, channel_id: int, contact_id: int) -> CallPermission | None:
    return (await session.scalars(select(CallPermission).where(
        CallPermission.channel_id == channel_id, CallPermission.contact_id == contact_id)
        .order_by(CallPermission.requested_at.desc(), CallPermission.id.desc()).limit(1))).first()


async def state(session: AsyncSession, conv: Conversation) -> dict:
    p = await latest(session, conv.channel_id, conv.contact_id)
    out = permission_out(p)
    now = utcnow()
    n24, n7 = await _recent_requests(session, conv.channel_id, conv.contact_id, now)
    out["can_request"] = not out["can_call"] and n24 < 1 and n7 < 2
    out["requests_24h"], out["requests_7d"] = n24, n7
    return out


async def _recent_requests(session: AsyncSession, channel_id: int, contact_id: int, now: datetime) -> tuple[int, int]:
    base = (CallPermission.channel_id == channel_id, CallPermission.contact_id == contact_id,
            CallPermission.request_message_id.is_not(None))
    n24 = await session.scalar(select(func.count()).where(*base, CallPermission.requested_at > now - timedelta(hours=24)))
    n7 = await session.scalar(select(func.count()).where(*base, CallPermission.requested_at > now - timedelta(days=7)))
    return n24 or 0, n7 or 0


async def _channel(session: AsyncSession, conv: Conversation) -> Channel:
    channel = await session.get(Channel, conv.channel_id)
    if not channel or channel.provider != "whatsapp_cloud":
        raise OutboundCallError("Las llamadas solo están disponibles en números de WhatsApp", "not_whatsapp", 422)
    if not channel.calling_enabled:
        raise OutboundCallError("Las llamadas están desactivadas en este número (Configuraciones → Plataforma)",
                                "calling_disabled")
    return channel


async def request_permission(session: AsyncSession, conv: Conversation, agent: Agent,
                             body: str | None = None) -> CallPermission:
    from app.service import system_note
    from app.voice.calls import calling_client

    channel = await _channel(session, conv)
    contact = await session.get(Contact, conv.contact_id)
    current = await latest(session, channel.id, contact.id)
    if is_valid(current):
        raise OutboundCallError("El cliente ya dio permiso para llamarlo", "already_granted")
    now = utcnow()
    n24, n7 = await _recent_requests(session, channel.id, contact.id, now)
    if n24 >= 1 or n7 >= 2:
        raise OutboundCallError("WhatsApp permite pedir permiso de llamada 1 vez cada 24 h y 2 veces cada 7 días a "
                                "un mismo cliente", "rate_limited", 429)
    client = await calling_client(session, channel)
    wamid = await client.request_permission(
        _address(contact), body or "¿Podemos llamarte por WhatsApp para atender tu solicitud?")
    perm = CallPermission(organization_id=conv.organization_id, channel_id=channel.id, contact_id=contact.id,
                          status="requested", requested_by=agent.id, request_message_id=wamid or None,
                          requested_at=now)
    session.add(perm)
    await system_note(session, conv, f"📞 {agent.name} pidió permiso para llamar al cliente por WhatsApp")
    await session.commit()
    await _broadcast_permission(conv, perm)
    return perm


async def on_permission_reply(session: AsyncSession, channel: Channel, contact: Contact, conv: Conversation,
                              reply: dict) -> CallPermission:
    """Respuesta del cliente (interactive.call_permission_reply)."""
    from app.service import system_note

    perm = await latest(session, channel.id, contact.id)
    if perm is None or perm.status not in ("requested",):
        perm = CallPermission(organization_id=channel.organization_id, channel_id=channel.id, contact_id=contact.id,
                              status="requested")
        session.add(perm)
    accepted = str(reply.get("response", "")).lower() == "accept"
    perm.status = "granted" if accepted else "denied"
    perm.permanent = bool(reply.get("is_permanent")) and accepted
    perm.responded_at = utcnow()
    exp = reply.get("expiration_timestamp")
    if accepted and not perm.permanent:
        perm.expires_at = (datetime.fromtimestamp(int(exp), UTC) if exp
                           else perm.responded_at + timedelta(days=TEMPORARY_DAYS))
    await system_note(session, conv, "📞 El cliente aceptó recibir llamadas" + (" (permanente)" if perm.permanent else "")
                      if accepted else "📞 El cliente no aceptó recibir llamadas")
    await session.commit()
    await _broadcast_permission(conv, perm)
    return perm


async def _broadcast_permission(conv: Conversation, perm: CallPermission) -> None:
    await hub.broadcast("call.permission", {"conversation_id": conv.id, "contact_id": conv.contact_id,
                                            **{k: v for k, v in permission_out(perm).items() if k != "id"}},
                        conv.organization_id)


async def start_call(session: AsyncSession, conv: Conversation, agent: Agent, sdp_offer: str) -> Call:
    from app.voice.calls import broadcast_call, calling_client, log_event

    channel = await _channel(session, conv)
    contact = await session.get(Contact, conv.contact_id)
    perm = await latest(session, channel.id, contact.id)
    if not is_valid(perm):
        raise OutboundCallError("El cliente no ha dado permiso para recibir llamadas: pídelo primero",
                                "no_permission")
    if await session.scalar(select(func.count()).where(
            Call.contact_id == contact.id, Call.status.in_(("ringing", "pre_accepted", "connected", "transferring")))):
        raise OutboundCallError("Ya hay una llamada en curso con este cliente", "busy")
    call = Call(organization_id=conv.organization_id, channel_id=channel.id, contact_id=contact.id,
                conversation_id=conv.id, direction="outbound", status="ringing", handled_by="agent",
                agent_id=agent.id, initiated_by_agent_id=agent.id, wa_call_id=f"pending:{conv.id}:{utcnow().timestamp()}")
    session.add(call)
    await session.flush()
    try:
        client = await calling_client(session, channel)
        call.wa_call_id = await client.connect(_address(contact), sdp_offer, callback_data=f"call:{call.id}")
    except Exception as e:
        await session.rollback()
        raise OutboundCallError(f"Meta no pudo iniciar la llamada: {e}"[:500], "meta_error", 502) from e
    await log_event(session, call.id, "connect_out", {"agent_id": agent.id})
    await session.commit()
    await session.refresh(call)
    await session.refresh(call, ["contact"])
    await broadcast_call(call)
    return call


# --- Webhook (field "calls") de llamadas iniciadas por la empresa -----------------------------------
async def on_business_connect(payload: dict) -> None:
    """El cliente contestó: llega el SDP answer → solo al asesor que llamó."""
    from app.db import SessionLocal
    from app.voice.calls import broadcast_call, log_event

    async with SessionLocal() as session:
        call = await session.scalar(select(Call).where(Call.wa_call_id == payload.get("id")))
        if not call:
            return
        sdp = (payload.get("session") or {}).get("sdp")
        await log_event(session, call.id, "connect", {"direction": "BUSINESS_INITIATED"})
        await session.commit()
        if sdp and call.agent_id:
            await hub.send_to_agent(call.organization_id, call.agent_id, "call.answer",
                                    {"call_id": call.id, "sdp": sdp})
        await session.refresh(call, ["contact"])
        await broadcast_call(call)


async def on_call_status(status: dict) -> None:
    """RINGING / ACCEPTED / REJECTED de una llamada saliente."""
    from app.db import SessionLocal
    from app.voice.calls import ACTIVE, broadcast_call, finish, log_event

    async with SessionLocal() as session:
        call = await session.scalar(select(Call).where(Call.wa_call_id == status.get("id")))
        if not call or call.direction != "outbound":
            return
        value = str(status.get("status", "")).upper()
        await log_event(session, call.id, f"status_{value.lower()}", {k: v for k, v in status.items() if k != "id"})
        if value == "ACCEPTED" and call.status in ("ringing", "pre_accepted"):
            call.status, call.answered_at = "connected", call.answered_at or utcnow()
            await session.commit()
            await session.refresh(call, ["contact"])
            await broadcast_call(call)
        elif value == "REJECTED" and call.status in ACTIVE:
            await session.refresh(call, ["contact"])
            await finish(session, call, "rejected", "El cliente rechazó la llamada")
        else:
            await session.commit()
