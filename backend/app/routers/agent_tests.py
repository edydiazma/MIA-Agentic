"""Pruebas de agentes de IA (Automatizaciones → Cortex → Pruebas de agentes). §12.3.

Suites por agente de IA con casos (conversación de entrada + expectativas). Correr una suite simula cada caso
con el agente en un arenero (app/quality/agent_tests.py) y guarda el resultado por caso.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import (
    Agent,
    AgentTestCase,
    AgentTestResult,
    AgentTestRun,
    AgentTestSuite,
    AIAgent,
    Conversation,
    utcnow,
)
from app.plans import feature_required
from app.quality.agent_tests import clean_expectations, clean_turns, execute_run, start_run, turns_from_conversation

router = APIRouter(prefix="/api/agent-tests", tags=["agent-tests"], dependencies=[Depends(feature_required("qa"))])
_tasks: set[asyncio.Task] = set()


class SuiteIn(BaseModel):
    ai_agent_id: int
    name: str
    description: str | None = None
    run_on_change: bool = True
    min_pass_pct: int = 80


class CaseIn(BaseModel):
    name: str
    turns: list[dict]
    expectations: dict = {}
    position: int = 0


class FromConversationIn(BaseModel):
    conversation_id: int
    name: str | None = None
    expectations: dict = {}


def suite_out(s: AgentTestSuite, last_run: AgentTestRun | None = None, cases: int | None = None) -> dict:
    return {"id": s.id, "ai_agent_id": s.ai_agent_id, "name": s.name, "description": s.description,
            "run_on_change": s.run_on_change, "min_pass_pct": s.min_pass_pct, "cases": cases,
            "last_run": run_out(last_run) if last_run else None, "created_at": s.created_at,
            "updated_at": s.updated_at}


def case_out(c: AgentTestCase) -> dict:
    return {"id": c.id, "suite_id": c.suite_id, "name": c.name, "turns": c.turns, "expectations": c.expectations,
            "source_conversation_id": c.source_conversation_id, "position": c.position, "created_at": c.created_at}


def run_out(r: AgentTestRun) -> dict:
    return {"id": r.id, "suite_id": r.suite_id, "ai_agent_id": r.ai_agent_id, "config_revision_id": r.config_revision_id,
            "trigger": r.trigger, "status": r.status, "total": r.total, "passed": r.passed,
            "pass_pct": float(r.pass_pct) if r.pass_pct is not None else None, "cost_usd": float(r.cost_usd or 0),
            "started_by": r.started_by, "started_at": r.started_at, "finished_at": r.finished_at}


def result_out(r: AgentTestResult, case_names: dict[int, str]) -> dict:
    return {"id": r.id, "case_id": r.case_id, "case_name": case_names.get(r.case_id), "passed": r.passed,
            "reply": r.reply, "checks": r.checks,
            "judge_score": float(r.judge_score) if r.judge_score is not None else None,
            "latency_ms": r.latency_ms, "ai_call_ids": list(r.ai_call_ids or [])}


async def _suite(session: AsyncSession, org: int, sid: int) -> AgentTestSuite:
    s = await session.get(AgentTestSuite, sid)
    if not s or s.organization_id != org:
        raise HTTPException(404, "Suite no encontrada")
    return s


async def _case(session: AsyncSession, org: int, cid: int) -> AgentTestCase:
    c = await session.get(AgentTestCase, cid)
    if not c or not await session.scalar(select(AgentTestSuite.id).where(
            AgentTestSuite.id == c.suite_id, AgentTestSuite.organization_id == org)):
        raise HTTPException(404, "Caso no encontrado")
    return c


async def _check_agent(session: AsyncSession, org: int, ai_agent_id: int) -> AIAgent:
    a = await session.get(AIAgent, ai_agent_id)
    if not a or a.organization_id != org:
        raise HTTPException(404, "Agente de IA no encontrado")
    return a


def _check_case(body: CaseIn) -> tuple[list, dict]:
    turns = clean_turns(body.turns)
    if not any(t["role"] == "user" for t in turns):
        raise HTTPException(422, "El caso necesita al menos un mensaje del cliente")
    if turns[-1]["role"] != "user":
        raise HTTPException(422, "El último mensaje debe ser del cliente (es lo que el agente responde)")
    return turns, clean_expectations(body.expectations)


# --- Suites ---------------------------------------------------------------------------
@router.get("/suites")
async def list_suites(ai_agent_id: int | None = None, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    q = select(AgentTestSuite).where(AgentTestSuite.organization_id == agent.organization_id)
    if ai_agent_id:
        q = q.where(AgentTestSuite.ai_agent_id == ai_agent_id)
    out = []
    for s in (await session.scalars(q.order_by(AgentTestSuite.id))).all():
        last = (await session.scalars(select(AgentTestRun).where(AgentTestRun.suite_id == s.id)
                                      .order_by(AgentTestRun.started_at.desc()).limit(1))).first()
        n = len((await session.scalars(select(AgentTestCase.id).where(AgentTestCase.suite_id == s.id))).all())
        out.append(suite_out(s, last, n))
    return out


@router.post("/suites")
async def create_suite(body: SuiteIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await _check_agent(session, agent.organization_id, body.ai_agent_id)
    if not body.name.strip():
        raise HTTPException(422, "La suite necesita un nombre")
    if await session.scalar(select(AgentTestSuite.id).where(AgentTestSuite.ai_agent_id == body.ai_agent_id,
                                                            AgentTestSuite.name == body.name.strip())):
        raise HTTPException(409, "Ese agente ya tiene una suite con ese nombre")
    s = AgentTestSuite(organization_id=agent.organization_id, ai_agent_id=body.ai_agent_id, name=body.name.strip()[:120],
                       description=(body.description or "").strip()[:1000] or None, run_on_change=body.run_on_change,
                       min_pass_pct=max(0, min(100, body.min_pass_pct)))
    session.add(s)
    await session.commit()
    return suite_out(s, cases=0)


@router.put("/suites/{sid}")
async def update_suite(sid: int, body: SuiteIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    s = await _suite(session, agent.organization_id, sid)
    s.name, s.description = body.name.strip()[:120], (body.description or "").strip()[:1000] or None
    s.run_on_change, s.min_pass_pct = body.run_on_change, max(0, min(100, body.min_pass_pct))
    s.updated_at = utcnow()
    await session.commit()
    return suite_out(s)


@router.delete("/suites/{sid}")
async def delete_suite(sid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await session.delete(await _suite(session, agent.organization_id, sid))
    await session.commit()
    return {"ok": True}


# --- Casos ----------------------------------------------------------------------------
@router.get("/suites/{sid}/cases")
async def list_cases(sid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    s = await _suite(session, agent.organization_id, sid)
    rows = (await session.scalars(select(AgentTestCase).where(AgentTestCase.suite_id == s.id)
                                  .order_by(AgentTestCase.position, AgentTestCase.id))).all()
    return [case_out(c) for c in rows]


@router.post("/suites/{sid}/cases")
async def create_case(sid: int, body: CaseIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    s = await _suite(session, agent.organization_id, sid)
    turns, exp = _check_case(body)
    c = AgentTestCase(suite_id=s.id, name=body.name.strip()[:160] or "Caso", turns=turns, expectations=exp,
                      position=body.position)
    session.add(c)
    await session.commit()
    return case_out(c)


@router.post("/suites/{sid}/cases/from-conversation")
async def case_from_conversation(sid: int, body: FromConversationIn, agent: Agent = Depends(require_admin),
                                 session: AsyncSession = Depends(get_session)):
    """Caso a partir de una conversación real: sus mensajes hasta el último del cliente."""
    s = await _suite(session, agent.organization_id, sid)
    conv = await session.get(Conversation, body.conversation_id)
    if not conv or conv.organization_id != agent.organization_id:
        raise HTTPException(404, "Conversación no encontrada")
    turns = await turns_from_conversation(session, conv.id)
    if not turns:
        raise HTTPException(422, "La conversación no tiene mensajes de texto del cliente")
    c = AgentTestCase(suite_id=s.id, name=(body.name or f"Conversación #{conv.id}").strip()[:160], turns=turns,
                      expectations=clean_expectations(body.expectations), source_conversation_id=conv.id)
    session.add(c)
    await session.commit()
    return case_out(c)


@router.put("/cases/{cid}")
async def update_case(cid: int, body: CaseIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    c = await _case(session, agent.organization_id, cid)
    c.turns, c.expectations = _check_case(body)
    c.name, c.position = body.name.strip()[:160] or c.name, body.position
    await session.commit()
    return case_out(c)


@router.delete("/cases/{cid}")
async def delete_case(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await session.delete(await _case(session, agent.organization_id, cid))
    await session.commit()
    return {"ok": True}


# --- Ejecuciones ------------------------------------------------------------------------
@router.post("/suites/{sid}/run")
async def run(sid: int, wait: bool = False, agent: Agent = Depends(require_admin),
              session: AsyncSession = Depends(get_session)):
    """Corre la suite. Por defecto en segundo plano (consultar /runs/{id}); wait=true espera el resultado."""
    s = await _suite(session, agent.organization_id, sid)
    if not await session.scalar(select(AgentTestCase.id).where(AgentTestCase.suite_id == s.id).limit(1)):
        raise HTTPException(422, "La suite no tiene casos")
    if await session.scalar(select(AgentTestRun.id).where(AgentTestRun.suite_id == s.id,
                                                          AgentTestRun.status == "running").limit(1)):
        raise HTTPException(409, "Esta suite ya se está corriendo")
    r = await start_run(session, s, "manual", agent.id)
    if wait:
        return run_out(await execute_run(r.id))
    task = asyncio.create_task(execute_run(r.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_out(r)


@router.get("/suites/{sid}/runs")
async def list_runs(sid: int, limit: int = 20, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    s = await _suite(session, agent.organization_id, sid)
    rows = (await session.scalars(select(AgentTestRun).where(AgentTestRun.suite_id == s.id)
                                  .order_by(AgentTestRun.started_at.desc()).limit(max(1, min(limit, 100))))).all()
    return [run_out(r) for r in rows]


@router.get("/runs/{rid}")
async def get_run(rid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    r = await session.get(AgentTestRun, rid)
    if not r or r.organization_id != agent.organization_id:
        raise HTTPException(404, "Ejecución no encontrada")
    results = (await session.scalars(select(AgentTestResult).where(AgentTestResult.run_id == r.id)
                                     .order_by(AgentTestResult.id))).all()
    names = dict((await session.execute(select(AgentTestCase.id, AgentTestCase.name)
                                        .where(AgentTestCase.suite_id == r.suite_id))).all())
    return {**run_out(r), "results": [result_out(x, names) for x in results]}
