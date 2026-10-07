"""El mejor vendedor: ranking de asesores, generación del playbook y creación del agente de IA."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.learning import create_agent_from_profile, rank_agents, start_seller_run
from app.models import Agent, AIAgent, Cortex, LearningRun, SellerProfile
from app.routers.bots import bot_out
from app.routers.memory import RunOut, run_out
from app.schemas import BotOut, UTCDateTime
from app.settings_store import add_revision
from app.plans import feature_required

router = APIRouter(prefix="/api/seller", tags=["seller"], dependencies=[Depends(feature_required("seller"))])


class RunIn(BaseModel):
    agent_ids: list[int] = []
    name: str = "Mejor vendedor"
    max_conversations: int | None = None
    cortex_id: int | None = None


class ProfileOut(BaseModel):
    id: int
    name: str
    status: str
    source_agent_ids: list[int]
    stats: dict | None
    playbook: dict
    system_prompt: str
    ai_agent_id: int | None
    run_id: int | None
    created_at: UTCDateTime


class ProfileUpdate(BaseModel):
    name: str | None = None
    playbook: dict | None = None
    system_prompt: str | None = None
    status: str | None = None


class CreateAgentIn(BaseModel):
    base_agent_id: int | None = None  # copia Cortex y mensaje de transferencia de este agente


def _out(p: SellerProfile) -> ProfileOut:
    return ProfileOut(id=p.id, name=p.name, status=p.status, source_agent_ids=list(p.source_agent_ids or []),
                      stats=p.stats, playbook=p.playbook or {}, system_prompt=p.system_prompt,
                      ai_agent_id=p.ai_agent_id, run_id=p.run_id, created_at=p.created_at)


async def _profile(session: AsyncSession, pid: int, org: int) -> SellerProfile:
    p = await session.get(SellerProfile, pid)
    if not p or p.organization_id != org:
        raise HTTPException(404, "Perfil no encontrado")
    return p


@router.get("/ranking")
async def ranking(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    return await rank_agents(session, agent.organization_id, start, end)


@router.post("/runs", response_model=RunOut)
async def start_run(body: RunIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if body.agent_ids:
        valid = set((await session.scalars(select(Agent.id).where(
            Agent.organization_id == agent.organization_id, Agent.id.in_(body.agent_ids)))).all())
        if set(body.agent_ids) - valid:
            raise HTTPException(422, "Hay asesores que no existen")
    if body.cortex_id:
        cx = await session.get(Cortex, body.cortex_id)
        if not cx or cx.organization_id != agent.organization_id:
            raise HTTPException(422, "Cortex inválido")
    return run_out(await start_seller_run(session, agent, body.model_dump(exclude_none=True)))


@router.get("/runs", response_model=list[RunOut])
async def list_runs(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(LearningRun).where(
        LearningRun.organization_id == agent.organization_id, LearningRun.type == "seller")
        .order_by(LearningRun.id.desc()).limit(50))).all()
    return [run_out(r) for r in rows]


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(SellerProfile).where(SellerProfile.organization_id == agent.organization_id)
                                  .order_by(SellerProfile.id.desc()))).all()
    return [_out(p) for p in rows]


@router.get("/profiles/{pid}", response_model=ProfileOut)
async def get_profile(pid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return _out(await _profile(session, pid, agent.organization_id))


@router.put("/profiles/{pid}", response_model=ProfileOut)
async def update_profile(pid: int, body: ProfileUpdate, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    p = await _profile(session, pid, agent.organization_id)
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in ("draft", "applied", "archived"):
        raise HTTPException(422, "Estado inválido")
    if "system_prompt" in data and not (data["system_prompt"] or "").strip():
        raise HTTPException(422, "El prompt es obligatorio")
    for k, v in data.items():
        setattr(p, k, v)
    await add_revision(session, agent.organization_id, "seller_profile", p.id,
                       {"name": p.name, "playbook": p.playbook, "system_prompt": p.system_prompt}, "human", agent.id)
    await session.commit()
    return _out(p)


@router.post("/profiles/{pid}/create-agent", response_model=BotOut)
async def create_agent(pid: int, body: CreateAgentIn | None = None, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    p = await _profile(session, pid, agent.organization_id)
    base = None
    if body and body.base_agent_id:
        base = await session.get(AIAgent, body.base_agent_id)
        if not base or base.organization_id != agent.organization_id:
            raise HTTPException(422, "Agente base inválido")
    else:
        base = await session.scalar(select(AIAgent).where(AIAgent.organization_id == agent.organization_id)
                                    .order_by(AIAgent.id).limit(1))
    bot = await create_agent_from_profile(session, p, base, agent.id)
    return await bot_out(session, bot)
