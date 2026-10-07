"""Voz como servicio aparte (docs/data-model.md §19.2).

VOICE_DISPATCH=local (por defecto): la sesión de medios del agente de voz corre en el mismo proceso (una réplica,
desarrollo, pruebas) — comportamiento anterior.

VOICE_DISPATCH=remote: los procesos ROLE=voice ejecutan aiortc + el puente con el modelo en tiempo real. Cada nodo
late en worker_heartbeats con capacity / active_load / endpoint. La API:
  1. elige el nodo de voz sano menos cargado (latido < NODE_STALE_S y carga < capacidad);
  2. fija calls.media_node_id y le reenvía la señalización por HTTP interno (POST {endpoint}/internal/voice/…,
     cabecera X-Voice-Token = VOICE_INTERNAL_TOKEN). Se eligió HTTP y no NOTIFY porque el puente con un asesor
     necesita la respuesta (SDP answer) en la misma petición;
  3. el nodo contesta la llamada en Meta (pre_accept / accept), registra la sesión y publica los eventos con el
     hub (NOTIFY llega a todas las réplicas de la API).
Si un nodo deja de latir con llamadas en curso, `voice_watchdog_loop` las marca fallidas y crea una alerta: el
audio no se puede migrar a otro nodo. Solo los nodos de voz necesitan network_mode host (UDP); la API no.
"""

import logging

import httpx
from sqlalchemy import select, text

from app.config import get_settings
from app.models import Call

log = logging.getLogger(__name__)
NODE_STALE_S = 45
ACTIVE = ("ringing", "pre_accepted", "connected", "transferring")


class VoiceNodeError(Exception):
    pass


def remote() -> bool:
    return get_settings().voice_dispatch == "remote"


async def pick_node(session) -> dict | None:
    """Nodo de voz sano con capacidad libre, el menos cargado (proporcionalmente)."""
    row = (await session.execute(text("""
        select worker_id, endpoint, capacity, active_load from public.worker_heartbeats
        where (role = 'voice' or (role = 'all' and endpoint is not null)) and endpoint is not null and last_beat_at > now() - make_interval(secs => :stale)
          and (capacity is null or active_load < capacity)
        order by active_load::float / greatest(coalesce(capacity, 1), 1), last_beat_at desc
        limit 1"""), {"stale": NODE_STALE_S})).mappings().first()
    return dict(row) if row else None


async def node_endpoint(session, node_id: str) -> str | None:
    return await session.scalar(text("""select endpoint from public.worker_heartbeats
        where worker_id = :w and (role = 'voice' or (role = 'all' and endpoint is not null)) and last_beat_at > now() - make_interval(secs => :stale)"""),
                                {"w": node_id, "stale": NODE_STALE_S})


def http_client() -> httpx.AsyncClient:
    """Las pruebas la reemplazan por un nodo simulado."""
    return httpx.AsyncClient(timeout=30)


async def _post(endpoint: str, path: str, body: dict) -> dict:
    s = get_settings()
    if not s.voice_internal_token:
        raise VoiceNodeError("Falta VOICE_INTERNAL_TOKEN para hablar con los nodos de voz")
    async with http_client() as http:
        r = await http.post(f"{endpoint.rstrip('/')}/internal/voice/{path}", json=body,
                            headers={"X-Voice-Token": s.voice_internal_token})
    if r.status_code >= 400:
        raise VoiceNodeError(f"Nodo de voz {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


# --- Operaciones (local o remoto) --------------------------------------------------------------------
async def start(session, call: Call, sdp_offer: str | None) -> None:
    """Contesta con el agente de voz en el nodo adecuado. Sin nodo disponible → suena en los asesores."""
    from app.voice import calls as signaling
    from app.voice.agent_runtime import ring_agents_instead, start_voice_agent

    if not remote():
        signaling.spawn(start_voice_agent(call.id, sdp_offer))
        return
    node = await pick_node(session)
    if node is None:
        await ring_agents_instead(call.id, sdp_offer, "No hay nodos de voz disponibles")
        return
    call.media_node_id = node["worker_id"]
    call.media_node_at = signaling.utcnow()
    await session.commit()
    try:
        await _post(node["endpoint"], "start", {"call_id": call.id, "sdp_offer": sdp_offer})
    except Exception as e:  # noqa: BLE001
        log.warning("El nodo de voz %s no tomó la llamada %s: %s", node["worker_id"], call.id, e)
        call.media_node_id = None
        await session.commit()
        await ring_agents_instead(call.id, sdp_offer, f"El nodo de voz no respondió ({type(e).__name__})")


async def bridge(session, call: Call, browser_sdp_offer: str) -> str:
    """Transferencia agente de voz → asesor: el nodo dueño de la sesión crea el puente y devuelve el SDP answer."""
    from app.voice.session import registry

    if not remote() or not call.media_node_id:
        media = registry.get(call.id)
        if not media:
            raise VoiceNodeError("La llamada ya no está activa")
        return await media.bridge_to_agent(browser_sdp_offer)
    endpoint = await node_endpoint(session, call.media_node_id)
    if not endpoint:
        raise VoiceNodeError("El nodo de voz de esta llamada no está disponible")
    data = await _post(endpoint, f"{call.id}/bridge", {"sdp": browser_sdp_offer})
    return data["sdp"]


async def stop(session, call: Call) -> None:
    """Detiene la sesión de medios (colgar / terminate). Nunca lanza."""
    from app.voice.session import registry

    if not remote() or not call.media_node_id:
        await registry.stop(call.id)
        return
    try:
        endpoint = await node_endpoint(session, call.media_node_id)
        if endpoint:
            await _post(endpoint, f"{call.id}/stop", {})
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo detener la sesión de la llamada %s en su nodo: %s", call.id, e)


def has_session(call: Call) -> bool:
    """¿Hay sesión de medios para puentear? (local: el registro; remoto: la llamada tiene nodo)."""
    from app.voice.session import registry

    if remote() and call.media_node_id:
        return True
    return registry.get(call.id) is not None


# --- Vigilancia: nodo caído con llamadas en curso ------------------------------------------------------
async def fail_orphaned_calls() -> int:
    from app.db import SessionLocal
    from app.service import create_alert
    from app.voice import calls as signaling

    async with SessionLocal() as session:
        alive = list((await session.scalars(text("""select worker_id from public.worker_heartbeats
            where (role = 'voice' or (role = 'all' and endpoint is not null)) and last_beat_at > now() - interval '90 seconds'"""))).all())
        rows = (await session.scalars(select(Call).where(
            Call.media_node_id.is_not(None), Call.status.in_(ACTIVE), Call.media_node_id.not_in(alive)))).all()
        for call in rows:
            await signaling.log_event(session, call.id, "voice_node_lost", {"node": call.media_node_id})
            await session.refresh(call, ["contact"])
            await signaling.finish(session, call, "failed", "El nodo de voz dejó de responder")
            await create_alert(session, call.organization_id, severity="critical", layer="voice", source="system",
                               title="Se cayó un nodo de voz con llamadas en curso",
                               description=f"Llamada {call.id} terminada (nodo {call.media_node_id}).",
                               ref=f"call:{call.id}")
        return len(rows)


async def voice_watchdog_loop() -> None:
    import asyncio

    while True:
        await asyncio.sleep(30)
        if not remote():
            continue
        try:
            n = await fail_orphaned_calls()
            if n:
                log.warning("Llamadas terminadas por nodo de voz caído: %s", n)
        except Exception:
            log.exception("Vigilancia de nodos de voz")
