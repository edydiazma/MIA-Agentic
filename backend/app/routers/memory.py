"""Memoria del negocio: ítems aprendidos (o manuales) que se aprueban antes de que los usen los agentes."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.learning import MEMORY_KINDS, start_memory_run
from app.models import Agent, Cortex, LearningRun, MemoryItem, utcnow
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/memory", tags=["memory"])
STATUSES = ("pending", "approved", "rejected")


class ItemOut(BaseModel):
    id: int
    kind: str
    title: str
    content: str
    status: str
    source: str
    confidence: float | None
    evidence_conversation_ids: list[int]
    run_id: int | None
    reviewed_by: int | None
    reviewed_at: UTCDateTime | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ItemIn(BaseModel):
    kind: str
    title: str
    content: str


class ItemUpdate(BaseModel):
    kind: str | None = None
    title: str | None = None
    content: str | None = None
    status: str | None = None


class RunIn(BaseModel):
    start: date | None = None
    end: date | None = None
    typifications: list[str] = []
    max_conversations: int | None = None
    cortex_id: int | None = None


class RunOut(BaseModel):
    id: int
    type: str
    status: str
    params: dict
    stats: dict | None
    error: str | None
    cortex_id: int | None
    started_at: UTCDateTime
    finished_at: UTCDateTime | None


def _item(m: MemoryItem) -> ItemOut:
    return ItemOut(id=m.id, kind=m.kind, title=m.title, content=m.content, status=m.status, source=m.source,
                   confidence=m.confidence, evidence_conversation_ids=list(m.evidence_conversation_ids or []),
                   run_id=m.run_id, reviewed_by=m.reviewed_by, reviewed_at=m.reviewed_at, created_at=m.created_at,
                   updated_at=m.updated_at)


def run_out(r: LearningRun) -> RunOut:
    return RunOut(id=r.id, type=r.type, status=r.status, params=r.params or {}, stats=r.stats, error=r.error,
                  cortex_id=r.cortex_id, started_at=r.started_at, finished_at=r.finished_at)


async def _get(session: AsyncSession, item_id: int, org: int) -> MemoryItem:
    m = await session.get(MemoryItem, item_id)
    if not m or m.organization_id != org:
        raise HTTPException(404, "Ítem de memoria no encontrado")
    return m


async def _check_title(session: AsyncSession, org: int, title: str, item_id: int | None = None) -> None:
    dup = await session.scalar(select(MemoryItem.id).where(
        MemoryItem.organization_id == org, func.lower(MemoryItem.title) == title.strip().lower(),
        MemoryItem.status != "rejected"))
    if dup and dup != item_id:
        raise HTTPException(409, "Ya existe un ítem con ese título")


@router.get("/items")
async def list_items(
    status: str | None = None, kind: str | None = None, q: str | None = None,
    offset: int = 0, limit: int = Query(default=100, le=500),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    stmt = select(MemoryItem).where(MemoryItem.organization_id == agent.organization_id)
    if status:
        stmt = stmt.where(MemoryItem.status == status)
    if kind:
        stmt = stmt.where(MemoryItem.kind == kind)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MemoryItem.title.ilike(like), MemoryItem.content.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    counts = dict((await session.execute(select(MemoryItem.status, func.count()).where(
        MemoryItem.organization_id == agent.organization_id).group_by(MemoryItem.status))).all())
    rows = (await session.scalars(stmt.order_by(MemoryItem.status != "pending", MemoryItem.id.desc())
                                  .offset(offset).limit(limit))).all()
    return {"total": total, "counts": counts, "kinds": MEMORY_KINDS, "items": [_item(m) for m in rows]}


@router.post("/items", response_model=ItemOut)
async def create_item(body: ItemIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Ítem escrito a mano: entra aprobado."""
    if body.kind not in MEMORY_KINDS:
        raise HTTPException(422, f"Tipo inválido: {', '.join(MEMORY_KINDS)}")
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(422, "Título y contenido son obligatorios")
    await _check_title(session, agent.organization_id, body.title)
    m = MemoryItem(organization_id=agent.organization_id, kind=body.kind, title=body.title.strip(),
                   content=body.content.strip(), status="approved", source="manual", confidence=1.0,
                   reviewed_by=agent.id, reviewed_at=utcnow())
    session.add(m)
    await session.commit()
    return _item(m)


@router.put("/items/{item_id}", response_model=ItemOut)
async def update_item(item_id: int, body: ItemUpdate, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    m = await _get(session, item_id, agent.organization_id)
    data = body.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] not in MEMORY_KINDS:
        raise HTTPException(422, "Tipo inválido")
    if "status" in data and data["status"] not in STATUSES:
        raise HTTPException(422, "Estado inválido")
    if data.get("title"):
        data["title"] = data["title"].strip()
    if data.get("title") or (data.get("status") and data["status"] != "rejected" and m.status == "rejected"):
        await _check_title(session, agent.organization_id, data.get("title") or m.title, m.id)
    for k, v in data.items():
        setattr(m, k, v)
    if "status" in data and data["status"] != "pending":
        m.reviewed_by, m.reviewed_at = agent.id, utcnow()
    await session.commit()
    return _item(m)


@router.post("/items/{item_id}/{action}", response_model=ItemOut)
async def review_item(item_id: int, action: str, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    if action not in ("approve", "reject"):
        raise HTTPException(404, "Acción desconocida")
    return await update_item(item_id, ItemUpdate(status="approved" if action == "approve" else "rejected"),
                             agent, session)


@router.post("/runs", response_model=RunOut)
async def start_run(body: RunIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if body.cortex_id:
        cx = await session.get(Cortex, body.cortex_id)
        if not cx or cx.organization_id != agent.organization_id:
            raise HTTPException(422, "Cortex inválido")
    params = body.model_dump(mode="json", exclude_none=True)
    return run_out(await start_memory_run(session, agent, params))


@router.get("/runs", response_model=list[RunOut])
async def list_runs(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(LearningRun).where(
        LearningRun.organization_id == agent.organization_id, LearningRun.type == "memory")
        .order_by(LearningRun.id.desc()).limit(50))).all()
    return [run_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    r = await session.get(LearningRun, run_id)
    if not r or r.organization_id != agent.organization_id:
        raise HTTPException(404, "Ejecución no encontrada")
    return run_out(r)
