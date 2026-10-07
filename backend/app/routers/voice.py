"""Agentes de voz (CRUD) y configuración de llamadas por número de WhatsApp."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, AIAgent, Channel, Group, VoiceAgent
from app.plans import feature_required
from app.settings_store import get_setting
from app.voice.calls import calling_client
from app.voice.meta import CallingError

router = APIRouter(prefix="/api/voice", tags=["voice"])
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PROVIDERS = ("openai_realtime", "pipeline")


class VoiceAgentIn(BaseModel):
    name: str
    enabled: bool = True
    ai_agent_id: int | None = None
    provider: str = "openai_realtime"
    model: str = "gpt-realtime"
    voice: str = "alloy"
    language: str = "es"
    greeting: str = "Hola, gracias por llamar. ¿En qué te puedo ayudar?"
    max_duration_s: int = Field(default=600, ge=30, le=3600)
    transfer_group_id: int | None = None
    record_calls: bool = True
    settings: dict = {}


class CallingHours(BaseModel):
    days: list[int] = [0, 1, 2, 3, 4]
    start: str = "08:00"
    end: str = "18:00"


class ChannelCallingIn(BaseModel):
    calling_enabled: bool
    calling_hours: CallingHours | None = None
    voice_agent_id: int | None = None
    sync_meta: bool = True  # aplicar también en Meta (POST /{phone-number-id}/settings)


def _va_out(v: VoiceAgent) -> dict:
    return {"id": v.id, "name": v.name, "enabled": v.enabled, "ai_agent_id": v.ai_agent_id, "provider": v.provider,
            "model": v.model, "voice": v.voice, "language": v.language, "greeting": v.greeting,
            "max_duration_s": v.max_duration_s, "transfer_group_id": v.transfer_group_id,
            "record_calls": v.record_calls, "settings": v.settings or {}}


def _channel_out(c: Channel) -> dict:
    return {"id": c.id, "name": c.name, "display_phone": c.display_phone, "phone_number_id": c.phone_number_id,
            "calling_enabled": c.calling_enabled, "calling_hours": c.calling_hours, "voice_agent_id": c.voice_agent_id}


async def _validate(session: AsyncSession, org: int, body: VoiceAgentIn) -> None:
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")
    if body.provider not in PROVIDERS:
        raise HTTPException(422, f"Proveedor inválido: {', '.join(PROVIDERS)}")
    if body.ai_agent_id:
        ai = await session.get(AIAgent, body.ai_agent_id)
        if not ai or ai.organization_id != org:
            raise HTTPException(422, "Agente de IA inválido")
    if body.transfer_group_id:
        g = await session.get(Group, body.transfer_group_id)
        if not g or g.organization_id != org:
            raise HTTPException(422, "Grupo inválido")


async def _va(session: AsyncSession, va_id: int, org: int) -> VoiceAgent:
    v = await session.get(VoiceAgent, va_id)
    if not v or v.organization_id != org:
        raise HTTPException(404, "Agente de voz no encontrado")
    return v


@router.get("/agents")
async def list_agents(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(VoiceAgent).where(VoiceAgent.organization_id == agent.organization_id)
                                  .order_by(VoiceAgent.id))).all()
    return [_va_out(v) for v in rows]


@router.post("/agents", dependencies=[Depends(feature_required("voice"))])
async def create_agent(body: VoiceAgentIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    await _validate(session, agent.organization_id, body)
    if await session.scalar(select(VoiceAgent.id).where(VoiceAgent.organization_id == agent.organization_id,
                                                        VoiceAgent.name == body.name.strip())):
        raise HTTPException(409, "Ya existe un agente de voz con ese nombre")
    v = VoiceAgent(organization_id=agent.organization_id, **{**body.model_dump(), "name": body.name.strip()})
    session.add(v)
    await session.commit()
    return _va_out(v)


@router.put("/agents/{va_id}", dependencies=[Depends(feature_required("voice"))])
async def update_agent(va_id: int, body: VoiceAgentIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    v = await _va(session, va_id, agent.organization_id)
    await _validate(session, agent.organization_id, body)
    for k, val in body.model_dump().items():
        setattr(v, k, val.strip() if k == "name" else val)
    await session.commit()
    return _va_out(v)


@router.delete("/agents/{va_id}")
async def delete_agent(va_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    v = await _va(session, va_id, agent.organization_id)
    await session.execute(update(Channel).where(Channel.voice_agent_id == v.id).values(voice_agent_id=None))
    await session.delete(v)
    await session.commit()
    return {"ok": True}


@router.get("/channels")
async def list_channels(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Channel).where(Channel.organization_id == agent.organization_id,
                                                        Channel.provider == "whatsapp_cloud").order_by(Channel.id))).unique().all()
    return [_channel_out(c) for c in rows]


@router.put("/channels/{channel_id}", dependencies=[Depends(feature_required("voice"))])
async def set_channel_calling(channel_id: int, body: ChannelCallingIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    c = await session.get(Channel, channel_id)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Número no encontrado")
    hours = body.calling_hours.model_dump() if body.calling_hours else None
    if hours:
        if not (HHMM.match(hours["start"]) and HHMM.match(hours["end"])) or hours["start"] >= hours["end"]:
            raise HTTPException(422, "Horario inválido (HH:MM, inicio antes del fin)")
        if not hours["days"] or any(d not in range(7) for d in hours["days"]):
            raise HTTPException(422, "Días inválidos (0 = lunes … 6 = domingo)")
    if body.voice_agent_id:
        await _va(session, body.voice_agent_id, agent.organization_id)
    meta_error = None
    if body.sync_meta:
        tz = (await get_setting(session, "company", agent.organization_id))["timezone"]
        try:
            await (await calling_client(session, c)).set_calling(body.calling_enabled, hours, tz)
        except CallingError as e:
            meta_error = str(e)[:500]
            if body.calling_enabled:
                raise HTTPException(502, f"Meta no aceptó la configuración de llamadas: {meta_error}") from None
    c.calling_enabled, c.calling_hours, c.voice_agent_id = body.calling_enabled, hours, body.voice_agent_id
    await session.commit()
    return {**_channel_out(c), "meta_error": meta_error}
