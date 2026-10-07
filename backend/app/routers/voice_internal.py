"""Señalización interna API → nodo de voz (ROLE=voice, o all con VOICE_DISPATCH=remote). §19.2

No se publica en Caddy (/internal no está en @backend); además exige la cabecera X-Voice-Token.
"""

import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(prefix="/internal/voice", tags=["voice-internal"], include_in_schema=False)


def _check(token: str | None) -> None:
    s = get_settings()
    if s.role not in ("voice", "all"):
        raise HTTPException(404, "No es un nodo de voz")
    if not s.voice_internal_token or not token or not hmac.compare_digest(token, s.voice_internal_token):
        raise HTTPException(401, "Token interno inválido")


class StartIn(BaseModel):
    call_id: int
    sdp_offer: str | None = None


class SdpIn(BaseModel):
    sdp: str


@router.post("/start")
async def start(body: StartIn, x_voice_token: str | None = Header(default=None)):
    """El nodo contesta con el agente de voz (o hace sonar a los asesores si no puede)."""
    _check(x_voice_token)
    from app.voice import calls as signaling
    from app.voice.agent_runtime import start_voice_agent

    signaling.spawn(start_voice_agent(body.call_id, body.sdp_offer))
    return {"accepted": True}


@router.post("/{call_id}/bridge")
async def bridge(call_id: int, body: SdpIn, x_voice_token: str | None = Header(default=None)):
    _check(x_voice_token)
    from app.voice.session import registry

    media = registry.get(call_id)
    if not media:
        raise HTTPException(409, "La llamada no tiene sesión en este nodo")
    return {"sdp": await media.bridge_to_agent(body.sdp)}


@router.post("/{call_id}/stop")
async def stop(call_id: int, x_voice_token: str | None = Header(default=None)):
    _check(x_voice_token)
    from app.voice.session import registry

    await registry.stop(call_id)
    return {"ok": True}
