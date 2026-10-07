"""Hub de WebSocket en memoria: empuja eventos a las bandejas abiertas, sabe qué asesores
están conectados y reenvía cada evento a los webhooks salientes.

Para varias réplicas del backend habría que reemplazarlo por Redis pub/sub.
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
        self.sockets: dict[WebSocket, int] = {}  # socket -> agent_id

    async def connect(self, ws: WebSocket, agent_id: int) -> None:
        await ws.accept()
        was_online = agent_id in self.online_agent_ids()
        self.sockets[ws] = agent_id
        if not was_online:
            await self.broadcast("agent.presence", {"agent_id": agent_id, "online": True})

    async def disconnect(self, ws: WebSocket) -> None:
        agent_id = self.sockets.pop(ws, None)
        if agent_id is not None and agent_id not in self.online_agent_ids():
            await self.broadcast("agent.presence", {"agent_id": agent_id, "online": False})

    def online_agent_ids(self) -> set[int]:
        return set(self.sockets.values())

    async def broadcast(self, event: str, data: dict) -> None:
        payload = json.dumps({"event": event, "data": data}, default=str)
        for ws in list(self.sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                log.debug("socket caído, se descarta")
                self.sockets.pop(ws, None)
        if event in PUBLIC_EVENTS:
            from app.webhooks_out import deliver

            asyncio.create_task(deliver(event, data))


hub = Hub()
