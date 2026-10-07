"""Eventos en vivo para el panel, con varias réplicas del backend (docs/data-model.md §12.1).

Cada réplica guarda sus propios WebSockets. `hub.broadcast(event, data, organization_id)`:
1. entrega en el acto a los sockets locales de esa empresa;
2. publica el evento con `pg_notify('wa_events', …)` (si pesa más de ~7,5 KB va a `realtime_spill` y el aviso
   lleva solo su id); las demás réplicas lo reciben por LISTEN y lo entregan a sus sockets de esa empresa
   (cada réplica ignora sus propios avisos);
3. una sola vez por evento —en la réplica que publica— lo envía a los webhooks salientes de la empresa y a las
   notificaciones push.

Aislamiento multiempresa (obligatorio): todo evento lleva organization_id y solo llega a sockets de esa
empresa; sin organización no se envía nada (falla cerrado).

Presencia en el clúster: cada réplica publica cada PRESENCE_EVERY segundos (y al conectarse/desconectarse un
asesor) la lista de (empresa, asesor) que tiene conectados; las demás la guardan con su hora y la descartan si
no se renueva en PRESENCE_TTL (réplica caída). online_agent_ids() = locales ∪ remotos vigentes.

REALTIME_MODE=local desactiva NOTIFY/LISTEN (un solo proceso, pruebas).

Panel en Supabase Realtime (REALTIME_TRANSPORT=supabase|both, §19.2): la réplica que publica también envía el
evento por la API de broadcast de Supabase al tema privado `org:{id}:events` (los de un asesor a
`org:{id}:agent:{agent_id}`) con el mismo {event, data} que el WebSocket; el navegador se suscribe con el token de
GET /api/realtime/token. Como el navegador ya no abre /ws, la presencia también cuenta las sesiones con latido
reciente (agent_sessions.last_seen_at, que el panel renueva con POST /api/me/heartbeat).
"""

import asyncio
import json
import logging
import os
import socket
import time
import uuid

from fastapi import WebSocket
from sqlalchemy import text

from app.config import get_settings
from app.ops import metrics

log = logging.getLogger(__name__)

CHANNEL = "wa_events"
MAX_NOTIFY_BYTES = 7500
PRESENCE_EVERY = 20.0
PRESENCE_TTL = 60.0
PRESENCE_EVENT = "_presence"  # interno: nunca llega a los sockets
SESSIONS_EVERY = 15.0  # presencia por latido del panel (transporte Supabase)
SESSIONS_FRESH_S = 60


def supabase_http():
    """Cliente HTTP para la API de broadcast de Supabase (las pruebas lo reemplazan)."""
    import httpx

    return httpx.AsyncClient(timeout=10)

# Eventos que también se entregan a webhooks salientes.
PUBLIC_EVENTS = {"message.new", "message.status", "conversation.updated", "conversation.handoff",
                 "conversation.closed", "appointment.created", "contact.updated"}


class Hub:
    def __init__(self, mode: str | None = None) -> None:
        self.mode = mode or get_settings().realtime_mode
        self.origin = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.sockets: dict[WebSocket, tuple[int, int]] = {}  # socket -> (organization_id, agent_id)
        self.remote: dict[str, tuple[float, set[tuple[int, int]]]] = {}  # origin -> (hora, {(org, agente)})
        self._listener: asyncio.Task | None = None
        self._presence: asyncio.Task | None = None
        self._conn = None  # conexión asyncpg del LISTEN
        self.listening = False
        self._tasks: set[asyncio.Task] = set()
        self.transport = get_settings().realtime_transport
        self.session_pairs: set[tuple[int, int]] = set()  # (empresa, asesor) con latido reciente del panel
        self._sessions: asyncio.Task | None = None

    # -- Sockets locales
    async def connect(self, ws: WebSocket, agent_id: int, organization_id: int) -> None:
        await ws.accept()
        was_online = agent_id in self.online_agent_ids(organization_id)
        self.sockets[ws] = (organization_id, agent_id)
        if not was_online:
            await self.broadcast("agent.presence", {"agent_id": agent_id, "online": True}, organization_id)
        self._spawn(self.publish_presence())

    async def disconnect(self, ws: WebSocket) -> None:
        entry = self.sockets.pop(ws, None)
        if entry:
            org, agent_id = entry
            if agent_id not in self.online_agent_ids(org):
                await self.broadcast("agent.presence", {"agent_id": agent_id, "online": False}, org)
            self._spawn(self.publish_presence())

    def _local_pairs(self) -> set[tuple[int, int]]:
        return set(self.sockets.values())

    def online_agent_ids(self, organization_id: int | None = None) -> set[int]:
        now = time.monotonic()
        pairs = self._local_pairs() | self.session_pairs
        for seen, remote in self.remote.values():
            if now - seen <= PRESENCE_TTL:
                pairs |= remote
        return {a for (o, a) in pairs if organization_id is None or o == organization_id}

    # -- Publicar
    async def broadcast(self, event: str, data: dict, organization_id: int | None) -> None:
        if organization_id is None:
            log.error("Evento %s sin organización: no se envía (aislamiento multiempresa)", event)
            return
        payload = json.dumps({"event": event, "data": data}, default=str)
        await self._deliver_local(organization_id, payload)
        if self.mode == "pg":
            await self._notify({"o": self.origin, "org": organization_id, "p": payload})
        else:
            metrics.realtime_published.inc("local")
        # Una sola vez por evento (en la réplica que publica)
        if event in PUBLIC_EVENTS:
            from app.webhooks_out import deliver

            self._spawn(deliver(event, data, organization_id=organization_id))
        self._spawn(self._push(event, data, organization_id))
        self._spawn(self._panel_notify(event, data, organization_id))
        if self.transport in ("supabase", "both"):
            self._spawn(self._supabase_publish(f"org:{organization_id}:events", event, data))

    async def send_to_agent(self, organization_id: int, agent_id: int, event: str, data: dict) -> None:
        """Evento privado de un asesor (p. ej. notification.new): solo a sus sockets, en cualquier réplica."""
        payload = json.dumps({"event": event, "data": data}, default=str)
        await self._deliver_local(organization_id, payload, agent_id)
        if self.mode == "pg":
            await self._notify({"o": self.origin, "org": organization_id, "a": agent_id, "p": payload})
        if self.transport in ("supabase", "both"):
            self._spawn(self._supabase_publish(f"org:{organization_id}:agent:{agent_id}", event, data))

    async def _supabase_publish(self, topic: str, event: str, data: dict) -> None:
        """POST {SUPABASE_URL}/realtime/v1/api/broadcast con la clave secreta (solo backend)."""
        s = get_settings()
        if not (s.supabase_url and s.supabase_secret_key):
            return
        body = {"messages": [{"topic": topic, "event": event, "private": True,
                              "payload": json.loads(json.dumps({"event": event, "data": data}, default=str))}]}
        try:
            async with supabase_http() as http:
                r = await http.post(f"{s.supabase_url.rstrip('/')}/realtime/v1/api/broadcast", json=body,
                                    headers={"apikey": s.supabase_secret_key,
                                             "Authorization": f"Bearer {s.supabase_secret_key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            metrics.realtime_published.inc("supabase")
        except Exception:
            metrics.realtime_errors.inc("supabase")
            log.warning("No se pudo publicar %s en Supabase Realtime", event, exc_info=True)

    async def refresh_session_presence(self) -> None:
        from app.db import engine

        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                select distinct organization_id, agent_id from public.agent_sessions
                where ended_at is null and last_seen_at > now() - make_interval(secs => :f)"""),
                {"f": SESSIONS_FRESH_S})).all()
        self.session_pairs = {(int(o), int(a)) for o, a in rows}

    async def _sessions_forever(self) -> None:
        while True:
            try:
                await self.refresh_session_presence()
            except Exception:
                log.debug("Presencia por latido no disponible", exc_info=True)
            await asyncio.sleep(SESSIONS_EVERY)

    async def _panel_notify(self, event: str, data: dict, organization_id: int) -> None:
        """Campana del panel (app/notifications.py): una vez por evento, en la réplica que publica."""
        try:
            from app.notifications import on_event
        except (ImportError, AttributeError):
            return
        try:
            await on_event(event, data, organization_id)
        except Exception:
            log.exception("Notificación del panel falló para %s", event)

    async def _push(self, event: str, data: dict, organization_id: int) -> None:
        try:
            from app.push import on_event
        except (ImportError, AttributeError):
            return
        try:
            await on_event(event, data, organization_id)
        except Exception:
            log.exception("Notificación push falló para %s", event)

    async def _deliver_local(self, organization_id: int, payload: str, agent_id: int | None = None) -> None:
        for ws, (org, agent) in list(self.sockets.items()):
            if org != organization_id or (agent_id is not None and agent != agent_id):
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                log.debug("socket caído, se descarta")
                self.sockets.pop(ws, None)

    async def _notify(self, message: dict) -> None:
        from app.db import engine

        body = json.dumps(message, default=str)
        try:
            async with engine.begin() as conn:
                if len(body.encode()) > MAX_NOTIFY_BYTES:
                    spill_id = await conn.scalar(text(
                        "insert into public.realtime_spill (payload) values (cast(:p as jsonb)) returning id"),
                        {"p": body})
                    body = json.dumps({"o": message["o"], "s": spill_id})
                    metrics.realtime_spilled.inc()
                await conn.execute(text("select pg_notify(:c, :b)"), {"c": CHANNEL, "b": body})
            metrics.realtime_published.inc("pg")
        except Exception:
            metrics.realtime_errors.inc("publish")
            log.exception("No se pudo publicar el evento en vivo a las demás réplicas")

    async def publish_presence(self) -> None:
        if self.mode != "pg":
            return
        pairs = sorted(self._local_pairs())
        await self._notify({"o": self.origin, "k": PRESENCE_EVENT, "pairs": pairs})

    # -- Recibir (LISTEN)
    async def _on_notify(self, _conn, _pid, _channel, body: str) -> None:
        try:
            msg = json.loads(body)
            if msg.get("o") == self.origin:
                return
            if "s" in msg:  # evento grande: se lee de realtime_spill
                from app.db import engine

                async with engine.connect() as conn:
                    payload = await conn.scalar(text("select payload from public.realtime_spill where id = :i"),
                                                {"i": msg["s"]})
                if payload is None:
                    return
                msg = payload if isinstance(payload, dict) else json.loads(payload)
            if msg.get("k") == PRESENCE_EVENT:
                self.remote[msg["o"]] = (time.monotonic(), {(int(o), int(a)) for o, a in msg.get("pairs") or []})
                return
            metrics.realtime_received.inc()
            await self._deliver_local(int(msg["org"]), msg["p"], int(msg["a"]) if msg.get("a") is not None else None)
        except Exception:
            metrics.realtime_errors.inc("receive")
            log.exception("Evento en vivo inválido")

    def _callback(self, conn, pid, channel, body) -> None:
        self._spawn(self._on_notify(conn, pid, channel, body))

    async def _listen_forever(self) -> None:
        from app.ops import pg

        delay = 1.0
        while True:
            try:
                self._conn = await pg.connect()
                await self._conn.add_listener(CHANNEL, self._callback)
                self.listening, delay = True, 1.0
                log.info("Escuchando eventos en vivo de otras réplicas (%s)", self.origin)
                await self.publish_presence()
                while not self._conn.is_closed():
                    await asyncio.sleep(10)
                    await self._conn.execute("select 1")  # detecta conexiones muertas (pooler, red)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                metrics.realtime_errors.inc("listen")
                log.warning("LISTEN desconectado (%s); reintento en %.0f s", e, delay)
            finally:
                self.listening = False
                if self._conn is not None and not self._conn.is_closed():
                    try:
                        await self._conn.close()
                    except Exception:
                        log.debug("cierre del LISTEN falló", exc_info=True)
                self._conn = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _presence_forever(self) -> None:
        while True:
            await asyncio.sleep(PRESENCE_EVERY)
            await self.publish_presence()
            now = time.monotonic()
            for origin, (seen, _pairs) in list(self.remote.items()):
                if now - seen > PRESENCE_TTL:
                    self.remote.pop(origin, None)

    async def start(self) -> None:
        if self.transport in ("supabase", "both") and self._sessions is None:
            self._sessions = asyncio.create_task(self._sessions_forever())
        if self.mode != "pg" or self._listener is not None:
            return
        self._listener = asyncio.create_task(self._listen_forever())
        self._presence = asyncio.create_task(self._presence_forever())

    async def stop(self) -> None:
        for t in (self._listener, self._presence, self._sessions):
            if t is not None:
                t.cancel()
        for t in (self._listener, self._presence, self._sessions):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._listener = self._presence = self._sessions = None

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


hub = Hub()

metrics.Gauge("wa_ws_connections", "WebSockets abiertos en esta réplica", fn=lambda: len(hub.sockets))
metrics.Gauge("wa_realtime_listening", "1 si el LISTEN de eventos entre réplicas está conectado",
              fn=lambda: 1 if hub.listening else 0)
