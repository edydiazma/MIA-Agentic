"""Diagnóstico de integraciones (go-live): Configuraciones → Diagnóstico y back-office de la plataforma. §20

Las verificaciones son de solo lectura. Si una corrida tarda más de unos segundos, el POST responde con
`status: running` y el panel consulta GET hasta que termine.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import Agent, SystemCheck, SystemCheckRun
from app.preflight.core import AREA_LABELS, AREAS, run_and_save, start_run
from app.tenancy import count_orgs

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["preflight"])
WAIT_S = 25.0
_tasks: set[asyncio.Task] = set()


class RunIn(BaseModel):
    areas: list[str] | None = None


def _run_out(run: SystemCheckRun | None) -> dict | None:
    if run is None:
        return None
    return {"id": run.id, "trigger": run.trigger, "app_version": run.app_version, "passed": run.passed,
            "warned": run.warned, "failed": run.failed, "skipped": run.skipped, "started_at": run.started_at,
            "finished_at": run.finished_at, "running": run.finished_at is None}


async def _latest(session: AsyncSession, org: int | None) -> dict:
    cond = SystemCheck.organization_id.is_(None) if org is None else SystemCheck.organization_id == org
    rows = (await session.scalars(select(SystemCheck).where(cond).order_by(SystemCheck.area, SystemCheck.check_key))).all()
    rcond = SystemCheckRun.organization_id.is_(None) if org is None else SystemCheckRun.organization_id == org
    run = await session.scalar(select(SystemCheckRun).where(rcond).order_by(SystemCheckRun.id.desc()).limit(1))
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.area, []).append({
            "check_key": r.check_key, "label": r.label, "status": r.status, "detail": r.detail, "data": r.data,
            "latency_ms": r.latency_ms, "checked_at": r.checked_at})
    return {"run": _run_out(run),
            "areas": [{"area": a, "label": AREA_LABELS[a], "results": groups[a]} for a in AREAS if a in groups]}


async def _start(org: int | None, scope: str, areas: list[str] | None, agent_id: int | None,
                 session: AsyncSession) -> tuple[SystemCheckRun, bool]:
    areas = [a for a in (areas or AREAS) if a in AREAS] or list(AREAS)
    run = await start_run(session, org, "manual", agent_id)
    task = asyncio.create_task(run_and_save(org, scope, areas, run=run))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), WAIT_S)
        return run, True
    except TimeoutError:
        return run, False


# --- Empresa ----------------------------------------------------------------------------------------------
@router.get("/preflight")
async def latest(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Último diagnóstico de la empresa. En una instalación de una sola empresa incluye también el de la plataforma
    (variables del servidor); en SaaS eso solo lo ve el back-office."""
    return await _org_view(session, agent.organization_id)


async def _org_view(session: AsyncSession, org: int) -> dict:
    out = await _latest(session, org)
    out["platform"] = await _latest(session, None) if await count_orgs(session) <= 1 else None
    return out


@router.post("/preflight/run")
async def run(body: RunIn | None = None, agent: Agent = Depends(require_admin),
              session: AsyncSession = Depends(get_session)):
    org, agent_id = agent.organization_id, agent.id  # antes de cualquier commit (los atributos caducan)
    areas = (body or RunIn()).areas
    run_row, done = await _start(org, "org", areas, agent_id, session)
    run_id = run_row.id
    if done and await count_orgs(session) <= 1:  # instalación propia: también las del servidor
        await _start(None, "platform", areas, agent_id, session)
    session.expire_all()
    out = await _org_view(session, org)
    out["status"] = "done" if done else "running"
    out["run_id"] = run_id
    return out


# --- Plataforma (back-office) -----------------------------------------------------------------------------
def _platform_admin_dep():
    from app.routers.platform import platform_admin

    return platform_admin


@router.get("/platform/preflight")
async def platform_latest(_=Depends(_platform_admin_dep()), session: AsyncSession = Depends(get_session)):
    return await _latest(session, None)


@router.post("/platform/preflight/run")
async def platform_run(body: RunIn | None = None, _=Depends(_platform_admin_dep()),
                       session: AsyncSession = Depends(get_session)):
    run_row, done = await _start(None, "platform", (body or RunIn()).areas, None, session)
    run_id = run_row.id
    session.expire_all()
    out = await _latest(session, None)
    out["status"] = "done" if done else "running"
    out["run_id"] = run_id
    return out
