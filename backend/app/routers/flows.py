"""Flujos: CRUD, versiones inmutables, publicación, catálogo de bloques, simulación y ejecuciones."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.flows.catalog import TRIGGERS, catalog
from app.flows.engine import simulate
from app.flows.validate import validate_definition
from app.models import Agent, Flow, FlowRun, FlowRunStep, FlowVersion, utcnow
from app.schemas import UTCDateTime
from app.plans import enforce_limit, has_feature

router = APIRouter(prefix="/api/flows", tags=["flows"])

STATUSES = ("draft", "active", "paused", "archived")


class FlowIn(BaseModel):
    name: str
    description: str | None = None
    trigger_type: str = "inbound_message"
    trigger_config: dict = {}
    editor_mode: str = "junior"
    priority: int = 100


class FlowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    editor_mode: str | None = None
    priority: int | None = None


class VersionIn(BaseModel):
    definition: dict
    change_note: str | None = None


class PublishIn(BaseModel):
    version_id: int | None = None


class TestIn(BaseModel):
    definition: dict | None = None
    text: str = ""


class VersionOut(BaseModel):
    id: int
    version: int
    change_note: str | None
    created_by_ai: bool
    ai_prompt: str | None
    created_at: UTCDateTime


def empty_definition(trigger_type: str, trigger_config: dict) -> dict:
    return {"schema_version": 1, "variables": [],
            "scripts": [{"id": "s1", "trigger": {"type": trigger_type, "config": trigger_config or {}}, "blocks": [],
                         "position": {"x": 40, "y": 40}}]}


async def _flow(session: AsyncSession, flow_id: int, org: int) -> Flow:
    flow = await session.get(Flow, flow_id)
    if not flow or flow.organization_id != org:
        raise HTTPException(404, "Flujo no encontrado")
    return flow


async def _latest(session: AsyncSession, flow_id: int) -> FlowVersion | None:
    return (await session.scalars(select(FlowVersion).where(FlowVersion.flow_id == flow_id)
                                  .order_by(FlowVersion.version.desc()).limit(1))).first()


def _out(flow: Flow) -> dict:
    v = flow.current_version
    return {"id": flow.id, "name": flow.name, "description": flow.description, "trigger_type": flow.trigger_type,
            "trigger_config": flow.trigger_config, "status": flow.status, "editor_mode": flow.editor_mode,
            "priority": flow.priority, "current_version_id": flow.current_version_id,
            "current_version": {"version": v.version, "created_at": v.created_at} if v else None,
            "updated_at": flow.updated_at}


async def save_version(session: AsyncSession, flow: Flow, definition: dict, agent_id: int | None,
                       change_note: str | None = None, created_by_ai: bool = False,
                       ai_prompt: str | None = None, ai_call_id: int | None = None) -> FlowVersion:
    """Valida y guarda una versión nueva (inmutable). La usa también la edición con IA."""
    errors = validate_definition(definition)
    if errors:
        raise HTTPException(422, {"message": "La definición tiene errores", "errors": errors})
    last = await session.scalar(select(func.max(FlowVersion.version)).where(FlowVersion.flow_id == flow.id))
    version = FlowVersion(flow_id=flow.id, version=(last or 0) + 1, definition=definition, change_note=change_note,
                          created_by_ai=created_by_ai, ai_prompt=ai_prompt, ai_call_id=ai_call_id, created_by=agent_id)
    session.add(version)
    # El disparador principal del flujo refleja el del primer script
    first = (definition.get("scripts") or [{}])[0].get("trigger") or {}
    if first.get("type") in TRIGGERS:
        flow.trigger_type, flow.trigger_config = first["type"], first.get("config") or {}
    flow.updated_at = utcnow()
    await session.flush()
    return version


@router.get("/catalog")
async def get_catalog(_: Agent = Depends(current_agent)):
    return catalog()


@router.get("")
async def list_flows(status: str | None = None, agent: Agent = Depends(current_agent),
                     session: AsyncSession = Depends(get_session)):
    stmt = select(Flow).where(Flow.organization_id == agent.organization_id).order_by(Flow.priority, Flow.id)
    if status:
        stmt = stmt.where(Flow.status == status)
    return [_out(f) for f in (await session.scalars(stmt)).unique().all()]


@router.post("")
async def create_flow(body: FlowIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await enforce_limit(session, agent.organization_id, "flows")
    if body.trigger_type not in TRIGGERS:
        raise HTTPException(422, f"Disparador inválido: {', '.join(TRIGGERS)}")
    if body.editor_mode not in ("junior", "advanced"):
        raise HTTPException(422, "Modo de editor inválido")
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")
    if await session.scalar(select(Flow.id).where(Flow.organization_id == agent.organization_id,
                                                  Flow.name == body.name.strip())):
        raise HTTPException(409, "Ya existe un flujo con ese nombre")
    flow = Flow(organization_id=agent.organization_id, name=body.name.strip(), description=body.description,
                trigger_type=body.trigger_type, trigger_config=body.trigger_config, editor_mode=body.editor_mode,
                priority=body.priority, created_by=agent.id)
    session.add(flow)
    await session.flush()
    session.add(FlowVersion(flow_id=flow.id, version=1, created_by=agent.id, change_note="Versión inicial",
                            definition=empty_definition(body.trigger_type, body.trigger_config)))
    await session.commit()
    await session.refresh(flow, ["current_version"])
    return _out(flow)


@router.get("/runs/{run_id}")
async def get_run(run_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    run = (await session.scalars(select(FlowRun).where(FlowRun.id == run_id,
                                                       FlowRun.organization_id == agent.organization_id))).first()
    if not run:
        raise HTTPException(404, "Ejecución no encontrada")
    steps = (await session.scalars(select(FlowRunStep).where(
        FlowRunStep.run_id == run.id, FlowRunStep.run_started_at == run.started_at).order_by(FlowRunStep.id))).all()
    return {**_run_out(run), "context": run.context,
            "steps": [{"id": s.id, "block_id": s.block_id, "type": s.block_type, "status": s.status, "input": s.input,
                       "output": s.output, "error": s.error, "latency_ms": s.latency_ms, "created_at": s.created_at}
                      for s in steps]}


def _run_out(r: FlowRun) -> dict:
    return {"id": r.id, "flow_id": r.flow_id, "flow_version_id": r.flow_version_id, "conversation_id": r.conversation_id,
            "trigger_type": r.trigger_type, "status": r.status, "error": r.error, "resume_at": r.resume_at,
            "started_at": r.started_at, "finished_at": r.finished_at, "steps": (r.context or {}).get("steps", 0)}


@router.get("/{flow_id}")
async def get_flow(flow_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    flow = await _flow(session, flow_id, agent.organization_id)
    latest = await _latest(session, flow.id)
    count = await session.scalar(select(func.count()).where(FlowVersion.flow_id == flow.id))
    return {**_out(flow), "definition": latest.definition if latest else None,
            "editing_version": {"id": latest.id, "version": latest.version} if latest else None,
            "versions_count": count}


@router.put("/{flow_id}")
async def update_flow(flow_id: int, body: FlowUpdate, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    flow = await _flow(session, flow_id, agent.organization_id)
    data = body.model_dump(exclude_unset=True)
    if "trigger_type" in data and data["trigger_type"] not in TRIGGERS:
        raise HTTPException(422, "Disparador inválido")
    if "editor_mode" in data and data["editor_mode"] not in ("junior", "advanced"):
        raise HTTPException(422, "Modo de editor inválido")
    for k, v in data.items():
        setattr(flow, k, v.strip() if isinstance(v, str) and k == "name" else v)
    await session.commit()
    return _out(flow)


@router.post("/{flow_id}/versions")
async def create_version(flow_id: int, body: VersionIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    flow = await _flow(session, flow_id, agent.organization_id)
    version = await save_version(session, flow, body.definition, agent.id, body.change_note)
    await session.commit()
    return {"id": version.id, "version": version.version, "errors": []}


@router.get("/{flow_id}/versions", response_model=list[VersionOut])
async def list_versions(flow_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await _flow(session, flow_id, agent.organization_id)
    rows = (await session.scalars(select(FlowVersion).where(FlowVersion.flow_id == flow_id)
                                  .order_by(FlowVersion.version.desc()))).all()
    return [VersionOut.model_validate(v, from_attributes=True) for v in rows]


@router.get("/{flow_id}/versions/{version_id}")
async def get_version(flow_id: int, version_id: int, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    await _flow(session, flow_id, agent.organization_id)
    v = await session.get(FlowVersion, version_id)
    if not v or v.flow_id != flow_id:
        raise HTTPException(404, "Versión no encontrada")
    return {"id": v.id, "version": v.version, "definition": v.definition, "change_note": v.change_note,
            "created_by_ai": v.created_by_ai, "ai_prompt": v.ai_prompt, "created_at": v.created_at}


@router.post("/{flow_id}/publish")
async def publish(flow_id: int, body: PublishIn | None = None, agent: Agent = Depends(require_admin),
                  session: AsyncSession = Depends(get_session)):
    flow = await _flow(session, flow_id, agent.organization_id)
    if not await has_feature(session, agent.organization_id, "flows"):
        raise HTTPException(402, "Tu plan no incluye «Flujos». Actualiza el plan para publicarlos.")
    version = await session.get(FlowVersion, body.version_id) if body and body.version_id else await _latest(session, flow.id)
    if not version or version.flow_id != flow.id:
        raise HTTPException(404, "Versión no encontrada")
    errors = validate_definition(version.definition)
    if errors:
        raise HTTPException(422, {"message": "La versión tiene errores", "errors": errors})
    if not any((s.get("blocks") or []) for s in version.definition.get("scripts") or []):
        raise HTTPException(422, "El flujo no tiene bloques")
    flow.current_version_id, flow.status = version.id, "active"
    await session.commit()
    await session.refresh(flow, ["current_version"])
    return _out(flow)


@router.post("/{flow_id}/test")
async def test_flow(flow_id: int, body: TestIn, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    """Simulación sin efectos: no envía mensajes ni escribe en la base."""
    flow = await _flow(session, flow_id, agent.organization_id)
    definition = body.definition or (await _latest(session, flow.id)).definition
    errors = validate_definition(definition)
    if errors:
        raise HTTPException(422, {"message": "La definición tiene errores", "errors": errors})
    sim = await simulate(definition, body.text)
    return {"steps": sim.steps, "messages": sim.messages, "waiting": sim.waiting}


@router.get("/{flow_id}/runs")
async def list_runs(flow_id: int, status: str | None = None, limit: int = Query(default=50, le=200),
                    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await _flow(session, flow_id, agent.organization_id)
    stmt = select(FlowRun).where(FlowRun.flow_id == flow_id).order_by(FlowRun.started_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(FlowRun.status == status)
    return [_run_out(r) for r in (await session.scalars(stmt)).all()]


# Debe ir al final: es una ruta genérica
@router.post("/{flow_id}/{action}")
async def change_status(flow_id: int, action: str, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    target = {"pause": "paused", "archive": "archived", "resume": "active"}.get(action)
    if not target:
        raise HTTPException(404, "Acción desconocida")
    flow = await _flow(session, flow_id, agent.organization_id)
    if target == "active" and not flow.current_version_id:
        raise HTTPException(422, "Publica una versión primero")
    flow.status = target
    await session.commit()
    return _out(flow)
