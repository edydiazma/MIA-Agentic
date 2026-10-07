"""Asistente del supervisor: hilos de chat con herramientas sobre los reportes. §19.3"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.copilot import assistant
from app.db import get_session
from app.models import Agent, AssistantMessage, AssistantThread

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _msg(m: AssistantMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "tool_calls": m.tool_calls, "charts": m.charts,
            "created_at": m.created_at}


def _thread(t: AssistantThread) -> dict:
    return {"id": t.id, "title": t.title, "created_at": t.created_at, "updated_at": t.updated_at}


async def _own(session: AsyncSession, agent: Agent, thread_id: int) -> AssistantThread:
    t = await session.get(AssistantThread, thread_id)
    if t is None or t.organization_id != agent.organization_id or t.agent_id != agent.id:
        raise HTTPException(404, "Conversación del asistente no encontrada")
    return t


@router.get("/threads")
async def threads(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await assistant.require_access(session, agent)
    rows = (await session.scalars(select(AssistantThread).where(
        AssistantThread.organization_id == agent.organization_id, AssistantThread.agent_id == agent.id)
        .order_by(AssistantThread.updated_at.desc()).limit(50))).all()
    return [_thread(t) for t in rows]


@router.get("/threads/{thread_id}")
async def thread(thread_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await assistant.require_access(session, agent)
    t = await _own(session, agent, thread_id)
    msgs = (await session.scalars(select(AssistantMessage).where(AssistantMessage.thread_id == t.id)
                                  .order_by(AssistantMessage.id))).all()
    return {**_thread(t), "messages": [_msg(m) for m in msgs]}


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    thread_id: int | None = None


@router.post("/ask")
async def ask(body: AskIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Pregunta en un hilo existente o en uno nuevo. Devuelve el hilo y los mensajes nuevos."""
    await assistant.require_access(session, agent)
    if body.thread_id:
        t = await _own(session, agent, body.thread_id)
    else:
        t = AssistantThread(organization_id=agent.organization_id, agent_id=agent.id)
        session.add(t)
        await session.flush()
    new = await assistant.ask(session, agent, t, body.text.strip())
    return {"thread": _thread(t), "messages": [_msg(m) for m in new]}


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: int, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    await assistant.require_access(session, agent)
    t = await _own(session, agent, thread_id)
    await session.delete(t)
    await session.commit()
    return {"ok": True}
