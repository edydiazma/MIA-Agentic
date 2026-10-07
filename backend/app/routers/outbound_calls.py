"""Llamar al cliente desde la conversación (§19.1): estado del permiso, pedirlo e iniciar la llamada."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import scope
from app.auth import current_agent
from app.db import get_session, set_actor
from app.models import Agent, Conversation
from app.voice import outbound
from app.voice.calls import call_out

router = APIRouter(prefix="/api/conversations", tags=["calls"])


class PermissionIn(BaseModel):
    body: str | None = None


class CallIn(BaseModel):
    sdp: str


async def _conv(session: AsyncSession, agent: Agent, conv_id: int) -> Conversation:
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.organization_id != agent.organization_id:
        raise HTTPException(404, "Conversación no encontrada")
    return await scope.require_conversation(session, agent, conv)


def _error(e: outbound.OutboundCallError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), "code": e.code})


@router.get("/{conv_id}/call-permission")
async def permission_state(conv_id: int, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, agent, conv_id)
    return await outbound.state(session, conv)


@router.post("/{conv_id}/call-permission")
async def request_permission(conv_id: int, body: PermissionIn, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, agent, conv_id)
    await set_actor(session, "agent", agent.id)
    try:
        await outbound.request_permission(session, conv, agent, body.body)
    except outbound.OutboundCallError as e:
        raise _error(e) from None
    return await outbound.state(session, conv)


@router.post("/{conv_id}/call")
async def start_call(conv_id: int, body: CallIn, agent: Agent = Depends(current_agent),
                     session: AsyncSession = Depends(get_session)):
    """El navegador del asesor envía su oferta SDP; el answer llega después por el evento call.answer."""
    conv = await _conv(session, agent, conv_id)
    await set_actor(session, "agent", agent.id)
    try:
        call = await outbound.start_call(session, conv, agent, body.sdp)
    except outbound.OutboundCallError as e:
        raise _error(e) from None
    return call_out(call)
