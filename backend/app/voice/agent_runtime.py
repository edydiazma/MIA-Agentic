"""Agente de voz con IA para llamadas de WhatsApp.

Medios: el backend actúa como par WebRTC (aiortc) frente a Meta y puentea el audio con un modelo de voz en
tiempo real (OpenAI Realtime por WebSocket):
  Meta (Opus 48 kHz) ─▶ aiortc ─▶ PCM16 24 kHz mono ─▶ input_audio_buffer.append
  response.output_audio.delta (PCM16 24 kHz) ─▶ pista de salida (48 kHz, tramas de 20 ms) ─▶ Meta
Transcripciones → call_turns. Herramienta transfer_to_human → suena en los asesores; si uno contesta, su
navegador se conecta a este servidor (segundo par WebRTC) y el audio se puentea cliente ↔ asesor.

Las dependencias pesadas (aiortc, av, websockets) se importan solo aquí, al atender una llamada.
"""

import asyncio
import base64
import fractions
import json
import logging
import os
import time

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AIAgent,
    AIAgentKnowledge,
    Call,
    Channel,
    Contact,
    KnowledgeDoc,
    MemoryItem,
    VoiceAgent,
    utcnow,
)
from app.voice import calls as signaling
from app.voice.recordings import RATE, Recorder, save_recording
from app.voice.session import registry

log = logging.getLogger(__name__)
settings = get_settings()
OUT_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms a 48 kHz
REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
# STUN para descubrir la IP pública (en EC2 los candidatos "host" son IP privadas). Vacío = solo candidatos host.
STUN_URLS = [u for u in os.environ.get("VOICE_STUN_URLS", "stun:stun.l.google.com:19302").split(",") if u.strip()]


def rtc_config():
    from aiortc import RTCConfiguration, RTCIceServer

    return RTCConfiguration(iceServers=[RTCIceServer(urls=u.strip()) for u in STUN_URLS])

VOICE_RULES = (
    "\n\n## Llamada telefónica\nEstás en una llamada de voz por WhatsApp. Habla en frases cortas y naturales, "
    "una idea a la vez, sin listas ni símbolos. Confirma datos importantes repitiéndolos. Si el cliente pide un "
    "asesor, está molesto o necesitas algo que no puedes resolver, usa la herramienta transfer_to_human. "
    "Nunca inventes precios ni políticas."
)
TRANSFER_TOOL = {
    "type": "function",
    "name": "transfer_to_human",
    "description": "Transfiere la llamada a un asesor humano. Antes, dile al cliente que lo vas a comunicar.",
    "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
}


async def build_instructions(session, call: Call, va: VoiceAgent) -> str:
    parts = []
    ai = await session.get(AIAgent, va.ai_agent_id) if va.ai_agent_id else None
    if ai:
        parts.append(ai.system_prompt)
        if ai.use_knowledge:
            docs = (await session.scalars(
                select(KnowledgeDoc).join(AIAgentKnowledge, AIAgentKnowledge.doc_id == KnowledgeDoc.id)
                .where(AIAgentKnowledge.ai_agent_id == ai.id, KnowledgeDoc.enabled))).all()
            if docs:
                kb = "\n\n".join(f"### {d.title}\n{d.content[:4000]}" for d in docs)[:16000]
                parts.append("## Base de conocimiento\n" + kb)
        if ai.use_memory:
            items = (await session.scalars(select(MemoryItem).where(
                MemoryItem.organization_id == call.organization_id, MemoryItem.status == "approved").limit(60))).all()
            if items:
                parts.append("## Memoria del negocio\n" + "\n".join(f"- {m.title}: {m.content}" for m in items))
    else:
        parts.append("Eres el asistente telefónico de la empresa. Ayuda al cliente con amabilidad.")
    contact = await session.get(Contact, call.contact_id)
    if contact:
        parts.append(f"## Cliente\nNombre: {contact.name or 'desconocido'}.")
        if contact.memory:
            parts.append("Lo que sabemos del cliente:\n" + contact.memory)
    parts.append(f"Responde en el idioma: {va.language}.")
    return "\n\n".join(parts) + VOICE_RULES


def _resample_24k_to_48k(pcm16: bytes) -> bytes:
    """Duplica cada muestra (24 → 48 kHz). Suficiente para voz; evita depender de numpy."""
    import array

    src = array.array("h")
    src.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    out = array.array("h", [0]) * (len(src) * 2)
    out[0::2] = src
    out[1::2] = src
    return out.tobytes()


class VoiceAgentSession:
    """Una llamada atendida por el agente de voz (implementa session.MediaSession)."""

    def __init__(self, call_id: int, org: int, va: VoiceAgent, instructions: str):
        self.call_id = call_id
        self.org = org
        self.va = va
        self.instructions = instructions
        self.recorder = Recorder(RATE) if va.record_calls else None
        self.out_buffer = bytearray()  # PCM16 48 kHz pendiente de enviar a Meta
        self.pc = None
        self.browser_pc = None
        self.ws = None
        self.tasks: list[asyncio.Task] = []
        self.bridged = False
        self.stopped = False
        self.started = time.monotonic()

    # --- WebRTC con Meta ---------------------------------------------------------
    async def start(self, sdp_offer: str) -> str:
        from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
        from aiortc.contrib.media import MediaRelay
        from av import AudioFrame

        session = self

        class OutgoingAudio(MediaStreamTrack):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self._t0 = None
                self._pts = 0

            async def recv(self):
                if self._t0 is None:
                    self._t0 = time.monotonic()
                wait = self._t0 + self._pts / OUT_RATE - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                size = FRAME_SAMPLES * 2
                chunk = bytes(session.out_buffer[:size])
                del session.out_buffer[:size]
                chunk = chunk.ljust(size, b"\x00")
                frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
                frame.planes[0].update(chunk)
                frame.sample_rate = OUT_RATE
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, OUT_RATE)
                self._pts += FRAME_SAMPLES
                return frame

        self.relay = MediaRelay()
        self.pc = RTCPeerConnection(rtc_config())
        self.pc.addTrack(OutgoingAudio())

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self.meta_track = track
                self.tasks.append(asyncio.create_task(self._pump_inbound(self.relay.subscribe(track))))

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp_offer, type="offer"))
        await self.pc.setLocalDescription(await self.pc.createAnswer())  # aiortc junta los candidatos ICE
        await self._connect_model()
        self.tasks.append(asyncio.create_task(self._max_duration()))
        return self.pc.localDescription.sdp

    async def _pump_inbound(self, track) -> None:
        from av import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while not self.stopped:
                frame = await track.recv()
                if self.bridged:
                    continue  # con un asesor, el audio va directo por el puente
                for f in resampler.resample(frame):
                    pcm = bytes(f.planes[0])[: f.samples * 2]
                    if self.recorder:
                        self.recorder.write("in", pcm)
                    if self.ws:
                        await self.ws.send(json.dumps({"type": "input_audio_buffer.append",
                                                       "audio": base64.b64encode(pcm).decode()}))
        except Exception as e:
            if not self.stopped:
                log.info("Fin del audio entrante de la llamada %s: %s", self.call_id, e)

    # --- Modelo de voz en tiempo real ------------------------------------------
    async def _connect_model(self) -> None:
        import websockets

        self.ws = await websockets.connect(REALTIME_URL.format(model=self.va.model),
                                           additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                                           max_size=None)
        await self.ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": self.instructions,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE},
                          "transcription": {"model": "gpt-4o-mini-transcribe", "language": self.va.language},
                          "turn_detection": {"type": "server_vad"}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.va.voice},
            },
            "tools": [TRANSFER_TOOL],
        }}))
        await self.ws.send(json.dumps({"type": "response.create", "response": {
            "instructions": f"Saluda al cliente exactamente así: «{self.va.greeting}»"}}))
        self.tasks.append(asyncio.create_task(self._pump_model()))

    async def _pump_model(self) -> None:
        try:
            async for raw in self.ws:
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t in ("response.output_audio.delta", "response.audio.delta"):
                    pcm = base64.b64decode(ev["delta"])
                    if self.recorder:
                        self.recorder.write("out", pcm, at_sample=self.recorder.position("in"))
                    self.out_buffer.extend(_resample_24k_to_48k(pcm))
                elif t == "input_audio_buffer.speech_started":
                    self.out_buffer.clear()  # el cliente interrumpe: se corta lo que estaba diciendo el agente
                elif t == "conversation.item.input_audio_transcription.completed":
                    await signaling.add_turn(self.call_id, "contact", ev.get("transcript", ""))
                elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                    await signaling.add_turn(self.call_id, "voice_agent", ev.get("transcript", ""))
                elif t == "response.function_call_arguments.done" and ev.get("name") == "transfer_to_human":
                    self.tasks.append(asyncio.create_task(self._transfer(ev.get("call_id"))))
                elif t == "error":
                    log.warning("Realtime (llamada %s): %s", self.call_id, ev.get("error"))
        except Exception as e:
            if not self.stopped:
                log.info("Se cerró el modelo de voz de la llamada %s: %s", self.call_id, e)

    async def _tool_result(self, tool_call_id: str | None, output: str, then: str) -> None:
        if not self.ws:
            return
        await self.ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": tool_call_id, "output": output}}))
        await self.ws.send(json.dumps({"type": "response.create", "response": {"instructions": then}}))

    async def _transfer(self, tool_call_id: str | None) -> None:
        ok = await signaling.request_transfer(self.call_id)
        if ok:
            return  # un asesor tomó la llamada: bridge_to_agent ya cortó el modelo
        await self._tool_result(tool_call_id, "No hay asesores disponibles en este momento.",
                                "Explica con amabilidad que no hay asesores disponibles ahora, que un asesor le "
                                "escribirá por WhatsApp, y ofrece seguir ayudando.")

    async def _max_duration(self) -> None:
        await asyncio.sleep(self.va.max_duration_s)
        if self.stopped or self.bridged:
            return
        if self.ws:
            await self.ws.send(json.dumps({"type": "response.create", "response": {
                "instructions": "Despídete brevemente: la llamada llegó a su duración máxima y un asesor le escribirá."}}))
        await asyncio.sleep(8)
        async with SessionLocal() as session:
            call = await session.get(Call, self.call_id)
            if call and call.status in signaling.ACTIVE:
                await signaling.hangup(session, call, "Duración máxima del agente de voz")

    # --- Transferencia a un asesor: segundo par WebRTC con su navegador ---------
    async def bridge_to_agent(self, browser_sdp_offer: str) -> str:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        self.bridged = True
        self.out_buffer.clear()
        if self.ws:
            await self.ws.close()
            self.ws = None
        self.browser_pc = RTCPeerConnection(rtc_config())
        self.browser_pc.addTrack(self.relay.subscribe(self.meta_track))  # cliente → asesor

        @self.browser_pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self.tasks.append(asyncio.create_task(self._pump_agent(track)))  # asesor → cliente

        await self.browser_pc.setRemoteDescription(RTCSessionDescription(sdp=browser_sdp_offer, type="offer"))
        await self.browser_pc.setLocalDescription(await self.browser_pc.createAnswer())
        return self.browser_pc.localDescription.sdp

    async def _pump_agent(self, track) -> None:
        from av import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
        try:
            while not self.stopped:
                frame = await track.recv()
                for f in resampler.resample(frame):
                    self.out_buffer.extend(bytes(f.planes[0])[: f.samples * 2])
        except Exception:
            pass

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for t in self.tasks:
            t.cancel()
        for closer in (self.ws, self.pc, self.browser_pc):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self.recorder:
            try:
                path = await save_recording(self.org, self.call_id, self.recorder)
                if path:
                    async with SessionLocal() as session:
                        call = await session.get(Call, self.call_id)
                        call.recording_path = path
                        await session.commit()
            except Exception:
                log.exception("No se pudo guardar la grabación de la llamada %s", self.call_id)


def runtime_available() -> bool:
    try:
        import aiortc  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        return False
    return bool(settings.openai_api_key)


async def create_media_session(call: Call, va: VoiceAgent, instructions: str):
    """Fábrica (las pruebas la reemplazan por una sesión simulada)."""
    return VoiceAgentSession(call.id, call.organization_id, va, instructions)


async def start_voice_agent(call_id: int, sdp_offer: str | None) -> None:
    """Contesta con el agente de voz; si no se puede (sin dependencias, sin clave o error), suena en los asesores."""
    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        va = await session.get(VoiceAgent, call.voice_agent_id)
        channel = await session.get(Channel, call.channel_id)
        await session.refresh(call)
        await session.refresh(call, ["contact"])
        fallback_reason = None
        if not sdp_offer:
            fallback_reason = "La llamada no trae oferta SDP"
        elif not runtime_available():
            fallback_reason = "Agente de voz no disponible (falta aiortc/websockets o la clave de OpenAI)"
        if not fallback_reason:
            try:
                media = await create_media_session(call, va, await build_instructions(session, call, va))
                answer = await media.start(sdp_offer)
                client = await signaling.calling_client(session, channel)
                await client.action(call.wa_call_id, "pre_accept", answer)
                await client.action(call.wa_call_id, "accept", answer, callback_data=f"call:{call.id}")
                registry.add(call.id, media)
                call.status, call.handled_by, call.answered_at = "connected", "voice_agent", utcnow()
                await signaling.log_event(session, call.id, "accept", {"by": "voice_agent", "voice_agent_id": va.id})
                await session.commit()
                await session.refresh(call)
                await session.refresh(call, ["contact"])
                await signaling.broadcast_call(call)
                return
            except Exception as e:
                log.exception("El agente de voz no pudo contestar la llamada %s", call_id)
                fallback_reason = f"Error del agente de voz: {type(e).__name__}"
                await registry.stop(call.id)
    await ring_agents_instead(call_id, sdp_offer, fallback_reason)


async def ring_agents_instead(call_id: int, sdp_offer: str | None, reason: str) -> None:
    """El agente de voz no puede contestar (sin dependencias, sin nodo de voz, error): suena en los asesores."""
    async with SessionLocal() as session:
        call = await session.get(Call, call_id)
        va = await session.get(VoiceAgent, call.voice_agent_id) if call.voice_agent_id else None
        await signaling.log_event(session, call.id, "voice_agent_fallback", {"reason": reason})
        await session.commit()
        await session.refresh(call)
        await session.refresh(call, ["contact"])
        targets = await signaling.eligible_agents(session, call.organization_id, va.transfer_group_id if va else None)
        await signaling.broadcast_call(call, "call.incoming", {"sdp_offer": sdp_offer, "targets": targets,
                                                               "mode": "answer"})
        signaling.spawn(signaling._ring_timeout(call.id))
