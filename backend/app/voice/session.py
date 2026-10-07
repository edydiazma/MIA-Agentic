"""Registro en memoria de las sesiones de medios activas (agente de voz / puente con un asesor).

Una sesión vive en el proceso del backend (despliegue de una réplica). Al colgar o al llegar el webhook
terminate se detiene con registry.stop(call_id).
"""

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class MediaSession(Protocol):
    async def stop(self) -> None: ...

    async def bridge_to_agent(self, browser_sdp_offer: str) -> str: ...


class Registry:
    def __init__(self) -> None:
        self.sessions: dict[int, MediaSession] = {}
        self._lock = asyncio.Lock()

    def add(self, call_id: int, session: MediaSession) -> None:
        self.sessions[call_id] = session

    def get(self, call_id: int) -> MediaSession | None:
        return self.sessions.get(call_id)

    async def stop(self, call_id: int) -> None:
        async with self._lock:
            s = self.sessions.pop(call_id, None)
        if s:
            try:
                await s.stop()
            except Exception:
                log.exception("Error deteniendo la sesión de medios de la llamada %s", call_id)


registry = Registry()
