"""Hub de WebSocket en memoria: empuja eventos a las bandejas abiertas de UNA empresa, sabe qué asesores
están conectados y reenvía cada evento a los webhooks salientes de esa misma empresa.

Aislamiento multiempresa (obligatorio): todo broadcast lleva organization_id y solo llega a los sockets
de esa organización. Sin organización no se envía nada (falla cerrado).

Para varias réplicas del backend habría que reemplazarlo por Supabase Realtime (los triggers ya publican
por canal privado de organización) o Redis pub/sub.
"""

import asyncio
import json
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)

# Eventos que también se entregan a webhooks salientes.
PUBLIC_EVENTS = {"message.new", "message.status", "conversation.updated", "conversation.handoff",
                 "conversation.closed", "appointment.created", "contact.updated"}


class Hub:
    def __init__(self) -> None:
        self.sockets: dict[WebSocket, tuple[int, int]] = {}  # socket -> (organization_id, agent_id)

    async def connect(self, ws: WebSocket, agent_id: int, organization_id: int) -> None:
        await ws.accept()
        was_online = agent_id in self.online_agent_ids(organization_id)
        self.sockets[ws] = (organization_id, agent_id)
        if not was_online:
            await self.broadcast("agent.presence", {"agent_id": agent_id, "online": True}, organization_id)

    async def disconnect(self, ws: WebSocket) -> None:
        entry = self.sockets.pop(ws, None)
        if entry:
            org, agent_id = entry
            if agent_id not in self.online_agent_ids(org):
                await self.broadcast("agent.presence", {"agent_id": agent_id, "online": False}, org)

    def online_agent_ids(self, organization_id: int | None = None) -> set[int]:
        return {a for (o, a) in self.sockets.values() if organization_id is None or o == organization_id}

    async def broadcast(self, event: str, data: dict, organization_id: int | None) -> None:
        if organization_id is None:
            log.error("Evento %s sin organización: no se envía (aislamiento multiempresa)", event)
            return
        payload = json.dumps({"event": event, "data": data}, default=str)
        for ws, (org, _agent) in list(self.sockets.items()):
            if org != organization_id:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                log.debug("socket caído, se descarta")
                self.sockets.pop(ws, None)
        if event in PUBLIC_EVENTS:
            from app.webhooks_out import deliver

            asyncio.create_task(deliver(event, data, organization_id=organization_id))


hub = Hub()
