"""Administración de la cola de trabajos de la empresa (§19.2): estado por cola, fallas recientes, reintentar y
cancelar. Solo trabajos de la empresa del administrador (los de plataforma, sin empresa, no se muestran)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import Agent

router = APIRouter(prefix="/api/ops/jobs", tags=["ops"])


@router.get("")
async def jobs_overview(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    queues = (await session.execute(text("""
        select queue,
               count(*) filter (where status = 'queued') as queued,
               count(*) filter (where status = 'running') as running,
               count(*) filter (where status = 'succeeded') as succeeded,
               count(*) filter (where status = 'failed') as failed,
               count(*) filter (where status = 'dead') as dead,
               coalesce(extract(epoch from now() - min(run_at) filter (where status = 'queued' and run_at <= now())), 0)
                 as oldest_ready_s
        from public.jobs where organization_id = :o group by queue order by queue"""), {"o": org})).mappings().all()
    failures = (await session.execute(text("""
        select id, queue, kind, status, attempts, max_attempts, last_error, run_at, finished_at, created_at
        from public.jobs where organization_id = :o and status in ('dead', 'failed', 'queued') and last_error is not null
        order by coalesce(finished_at, run_at) desc limit 50"""), {"o": org})).mappings().all()
    return {"queues": [{**dict(q), "oldest_ready_s": round(float(q["oldest_ready_s"]), 1)} for q in queues],
            "recent_failures": [dict(f) for f in failures]}


async def _job(session: AsyncSession, org: int, job_id: int) -> dict:
    row = (await session.execute(text("select id, status from public.jobs where id = :i and organization_id = :o"),
                                 {"i": job_id, "o": org})).mappings().first()
    if not row:
        raise HTTPException(404, "Trabajo no encontrado")
    return dict(row)


@router.post("/{job_id}/retry")
async def retry_job(job_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    row = await _job(session, agent.organization_id, job_id)
    if row["status"] not in ("dead", "failed", "cancelled", "queued"):
        raise HTTPException(409, "Solo se reintentan trabajos fallidos, muertos, cancelados o en espera")
    try:
        await session.execute(text("""update public.jobs set status = 'queued', attempts = 0, run_at = now(),
                                       finished_at = null, locked_by = null, locked_until = null where id = :i"""),
                              {"i": job_id})
        await session.commit()
    except IntegrityError:  # ya hay uno igual pendiente (misma dedupe_key)
        await session.rollback()
        raise HTTPException(409, "Ya hay un trabajo igual pendiente") from None
    return {"id": job_id, "status": "queued"}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    row = await _job(session, agent.organization_id, job_id)
    if row["status"] != "queued":
        raise HTTPException(409, "Solo se cancelan trabajos en espera")
    await session.execute(text("update public.jobs set status = 'cancelled', finished_at = now() where id = :i"),
                          {"i": job_id})
    await session.commit()
    return {"id": job_id, "status": "cancelled"}
