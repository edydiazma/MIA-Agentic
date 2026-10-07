"""Notificaciones Web Push de la app del asesor (PWA). docs/data-model.md §12.4.

`on_event(event, data, organization_id)` lo llama la réplica que publica cada evento (una sola vez por evento).
Decide a quién avisar según las preferencias de cada suscripción:
- assigned: se le asignó una conversación (no avisa si se la asignó él mismo).
- message_assigned: mensaje nuevo del cliente en su conversación y el asesor NO está conectado.
- call: llamada entrante de WhatsApp que suena para él.
- new_conversation: transferencia a su grupo sin asesor asignado (desactivado por defecto).
Nunca lanza errores hacia quien publica; el envío ocurre en segundo plano. Suscripciones con 404/410 se borran.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import AgentGroup, Conversation, ConversationEvent, PushSubscription, utcnow

log = logging.getLogger(__name__)
MAX_FAILURES = 10
_tasks: set[asyncio.Task] = set()
_seen: OrderedDict[int, None] = OrderedDict()  # eventos de asignación ya notificados (por réplica)


class PushGone(Exception):
    """La suscripción ya no existe (404/410)."""


def _webpush_send(subscription: dict, payload: str) -> None:
    """Envío real con pywebpush (síncrono: se ejecuta en un hilo)."""
    from pywebpush import WebPushException, webpush

    env = get_settings()
    try:
        webpush(subscription_info=subscription, data=payload, vapid_private_key=env.vapid_private_key,
                vapid_claims={"sub": env.vapid_subject}, ttl=3600)
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            raise PushGone(str(e)) from e
        raise


SENDER = _webpush_send  # las pruebas lo reemplazan


def enabled() -> bool:
    env = get_settings()
    return bool(env.vapid_public_key and env.vapid_private_key)


async def on_event(event: str, data: dict, organization_id: int | None) -> asyncio.Task | None:
    """Programa el aviso en segundo plano y regresa de inmediato; nunca lanza errores."""
    if organization_id is None or event not in ("conversation.updated", "message.new", "call.incoming",
                                                "conversation.handoff"):
        return None
    task = asyncio.get_running_loop().create_task(_safe(event, data, organization_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _safe(event: str, data: dict, org: int) -> int:
    try:
        return await handle(event, data, org)
    except Exception:
        log.exception("Falló el aviso push de %s", event)
        return 0


async def handle(event: str, data: dict, org: int) -> int:
    """Calcula destinatarios y envía. Devuelve cuántos avisos se enviaron (para pruebas)."""
    if not enabled() and SENDER is _webpush_send:
        return 0
    async with SessionLocal() as session:
        if event == "conversation.updated":
            return await _on_assigned(session, data, org)
        if event == "message.new":
            return await _on_message(session, data, org)
        if event == "call.incoming":
            targets = [t for t in (data.get("targets") or []) if isinstance(t, int)]
            caller = (data.get("contact") or {}).get("name") or data.get("from") or "un cliente"
            return await send(session, org, targets, "call", {
                "title": "Llamada entrante", "body": f"Te llama {caller}", "tag": f"call-{data.get('id')}",
                "url": "/conversaciones" + (f"?id={data['conversation_id']}" if data.get("conversation_id") else ""),
                "requireInteraction": True})
        if event == "conversation.handoff" and not data.get("assigned_agent") and data.get("group"):
            members = list((await session.scalars(select(AgentGroup.agent_id).where(
                AgentGroup.group_id == data["group"]["id"]))).all())
            return await send(session, org, members, "new_conversation", {
                "title": f"Nueva conversación en {data['group'].get('name', 'tu grupo')}",
                "body": _contact_name(data), "tag": f"conv-{data.get('id')}", "url": f"/conversaciones?id={data.get('id')}"})
    return 0


def _contact_name(data: dict) -> str:
    c = data.get("contact") or {}
    return c.get("name") or c.get("wa_id") or "Cliente"


async def _on_assigned(session, data: dict, org: int) -> int:
    agent = (data.get("assigned_agent") or {}).get("id")
    conv_id = data.get("id")
    if not agent or not conv_id:
        return 0
    ev = (await session.scalars(select(ConversationEvent).where(
        ConversationEvent.conversation_id == conv_id, ConversationEvent.event_type == "assigned",
        ConversationEvent.occurred_at >= utcnow() - timedelta(minutes=2))
        .order_by(ConversationEvent.occurred_at.desc()).limit(1))).first()
    if not ev or ev.assigned_agent_id != agent or ev.actor_agent_id == agent or ev.id in _seen:
        return 0  # sin asignación reciente, o se la tomó él mismo, o ya se avisó
    _seen[ev.id] = None
    while len(_seen) > 5000:
        _seen.popitem(last=False)
    return await send(session, org, [agent], "assigned", {
        "title": "Te asignaron una conversación", "body": _contact_name(data), "tag": f"conv-{conv_id}",
        "url": f"/conversaciones?id={conv_id}"})


async def _on_message(session, data: dict, org: int) -> int:
    if data.get("direction") != "in" or not data.get("conversation_id"):
        return 0
    conv = await session.get(Conversation, data["conversation_id"])
    if not conv or conv.organization_id != org or not conv.assigned_agent_id or conv.status == "closed":
        return 0
    from app.realtime import hub

    if conv.assigned_agent_id in hub.online_agent_ids(org):
        return 0  # lo ve en el panel
    text = data.get("text") or data.get("transcript") or {"image": "📷 Imagen", "audio": "🎤 Audio",
                                                          "document": "📄 Documento"}.get(data.get("type"), "Mensaje")
    return await send(session, org, [conv.assigned_agent_id], "message_assigned", {
        "title": conv.contact.name or conv.contact.wa_id or "Cliente", "body": text[:140], "tag": f"conv-{conv.id}",
        "url": f"/conversaciones?id={conv.id}"})


async def send(session, org: int, agent_ids: list[int], preference: str | None, notification: dict) -> int:
    """preference None = sin filtrar (aviso de prueba)."""
    if not agent_ids:
        return 0
    subs = (await session.scalars(select(PushSubscription).where(
        PushSubscription.organization_id == org, PushSubscription.agent_id.in_(set(agent_ids))))).all()
    sent = 0
    payload = json.dumps(notification, ensure_ascii=False)
    for sub in subs:
        if preference and not (sub.preferences or {}).get(preference, False):
            continue
        info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
        try:
            await asyncio.to_thread(SENDER, info, payload)
            sub.failures, sub.last_success_at = 0, utcnow()
            sent += 1
        except PushGone:
            await session.delete(sub)
        except Exception as e:
            sub.failures += 1
            log.warning("Push falló para la suscripción %s: %s", sub.id, e)
            if sub.failures >= MAX_FAILURES:
                await session.delete(sub)
    await session.commit()
    return sent
