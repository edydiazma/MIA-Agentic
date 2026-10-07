"""Webhooks entrantes: administración en el panel (Automatizaciones → Webhooks entrantes). docs/data-model.md §17"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import inbound_hooks as hooks
from app.auth import current_agent, require_admin
from app.config import get_settings
from app.db import get_session
from app.models import Agent, Channel, Flow, Group, InboundWebhook, InboundWebhookRun, utcnow
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/inbound-webhooks", tags=["inbound-webhooks"])


class ParamIn(BaseModel):
    name: str
    label: str | None = None
    type: str = "text"
    required: bool = False
    example: str | float | int | None = None
    maps_to: str | None = None


class HookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    action: str
    channel_id: int | None = None
    template_name: str | None = None
    template_language: str | None = None
    flow_id: int | None = None
    params: list[ParamIn] = []
    options: dict = {}


class HookOut(BaseModel):
    id: int
    url: str
    name: str
    slug: str
    status: str
    action: str
    channel_id: int | None
    template_name: str | None
    template_language: str | None
    flow_id: int | None
    params: list[dict]
    params_count: int
    options: dict
    version: int
    executions: int
    succeeded: int
    failed: int
    last_run_at: UTCDateTime | None
    created_by: dict | None
    published_at: UTCDateTime | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    token: str | None = None  # solo al crear, rotar o duplicar


def hook_url(slug: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/hooks/{slug}"


async def hook_out(session: AsyncSession, h: InboundWebhook, token: str | None = None) -> HookOut:
    creator = await session.get(Agent, h.created_by) if h.created_by else None
    return HookOut(id=h.id, url=hook_url(h.slug), name=h.name, slug=h.slug, status=h.status, action=h.action,
                   channel_id=h.channel_id, template_name=h.template_name, template_language=h.template_language,
                   flow_id=h.flow_id, params=list(h.params or []), params_count=hooks.params_count(h),
                   options=dict(h.options or {}), version=h.version, executions=h.executions, succeeded=h.succeeded,
                   failed=h.failed, last_run_at=h.last_run_at,
                   created_by={"id": creator.id, "name": creator.name} if creator else None,
                   published_at=h.published_at, created_at=h.created_at, updated_at=h.updated_at, token=token)


async def _get(session: AsyncSession, hook_id: int, org: int) -> InboundWebhook:
    h = await session.get(InboundWebhook, hook_id)
    if not h or h.organization_id != org:
        raise HTTPException(404, "Webhook no encontrado")
    return h


async def _validate(session: AsyncSession, org: int, body: HookIn, hook_id: int | None) -> list[dict]:
    try:
        params = hooks.validate_config(body.action, [p.model_dump() for p in body.params], body.options)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    dup = await session.scalar(select(InboundWebhook.id).where(InboundWebhook.organization_id == org,
                                                               InboundWebhook.name == body.name.strip()))
    if dup and dup != hook_id:
        raise HTTPException(409, "Ya existe un webhook con ese nombre")
    if body.channel_id is not None:
        ch = await session.get(Channel, body.channel_id)
        if not ch or ch.organization_id != org:
            raise HTTPException(422, "Número inválido")
    if body.action == "send_template" and not body.template_name:
        raise HTTPException(422, "Elige la plantilla a enviar")
    if body.action == "start_flow":
        flow = await session.get(Flow, body.flow_id) if body.flow_id else None
        if not flow or flow.organization_id != org:
            raise HTTPException(422, "Elige un flujo válido")
    opts = body.options or {}
    if opts.get("assign_group_id"):
        g = await session.get(Group, int(opts["assign_group_id"]))
        if not g or g.organization_id != org:
            raise HTTPException(422, "Grupo inválido")
    if opts.get("assign_agent_id"):
        a = await session.get(Agent, int(opts["assign_agent_id"]))
        if not a or a.organization_id != org:
            raise HTTPException(422, "Asesor inválido")
    return params


def _apply(h: InboundWebhook, body: HookIn, params: list[dict]) -> None:
    h.name, h.action = body.name.strip(), body.action
    h.channel_id, h.flow_id = body.channel_id, body.flow_id
    h.template_name = (body.template_name or "").strip() or None
    h.template_language = (body.template_language or "").strip() or None
    h.params, h.options = params, dict(body.options or {})


@router.get("", response_model=list[HookOut])
async def list_hooks(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(InboundWebhook).where(InboundWebhook.organization_id == agent.organization_id)
                                  .order_by(InboundWebhook.published_at.desc().nulls_last(), InboundWebhook.id.desc()))
            ).all()
    return [await hook_out(session, h) for h in rows]


@router.get("/template-params")
async def template_params(channel_id: int, name: str, language: str | None = None,
                          agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Parámetros sugeridos a partir de las variables de una plantilla aprobada."""
    ch = await session.get(Channel, channel_id)
    if not ch or ch.organization_id != agent.organization_id:
        raise HTTPException(404, "Número no encontrado")
    try:
        return await hooks.params_from_template(session, ch, name, language)
    except hooks.HookError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/{hook_id}", response_model=HookOut)
async def get_hook(hook_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await hook_out(session, await _get(session, hook_id, agent.organization_id))


@router.post("", response_model=HookOut)
async def create_hook(body: HookIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Crea el webhook en borrador. El token se muestra una sola vez."""
    org = agent.organization_id
    params = await _validate(session, org, body, None)
    token = hooks.new_token()
    h = InboundWebhook(organization_id=org, name=body.name.strip(), slug=await hooks.unique_slug(session, body.name),
                       token_hash=hooks.hash_token(token), status="draft", action=body.action, created_by=agent.id)
    _apply(h, body, params)
    session.add(h)
    await session.commit()
    return await hook_out(session, h, token)


@router.put("/{hook_id}", response_model=HookOut)
async def update_hook(hook_id: int, body: HookIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    params = await _validate(session, agent.organization_id, body, h.id)
    _apply(h, body, params)
    h.updated_at = utcnow()
    await session.commit()
    return await hook_out(session, h)


@router.post("/{hook_id}/publish", response_model=HookOut)
async def publish(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    if h.published_at is not None:  # republicar = versión nueva (las ejecuciones guardan la versión en su clave)
        h.version = (h.version or 1) + 1
    h.status, h.published_at = "active", utcnow()
    await session.commit()
    return await hook_out(session, h)


@router.post("/{hook_id}/pause", response_model=HookOut)
async def pause(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    h.status = "paused"
    await session.commit()
    return await hook_out(session, h)


@router.post("/{hook_id}/activate", response_model=HookOut)
async def activate(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    h.status = "active"
    h.published_at = h.published_at or utcnow()
    await session.commit()
    return await hook_out(session, h)


@router.post("/{hook_id}/rotate-token", response_model=HookOut)
async def rotate(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    token = hooks.new_token()
    h.token_hash = hooks.hash_token(token)
    await session.commit()
    return await hook_out(session, h, token)


@router.post("/{hook_id}/duplicate", response_model=HookOut)
async def duplicate(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    src = await _get(session, hook_id, agent.organization_id)
    name = f"{src.name} (copia)"
    n = 2
    while await session.scalar(select(InboundWebhook.id).where(InboundWebhook.organization_id == src.organization_id,
                                                               InboundWebhook.name == name)):
        name, n = f"{src.name} (copia {n})", n + 1
    token = hooks.new_token()
    h = InboundWebhook(organization_id=src.organization_id, name=name, slug=await hooks.unique_slug(session, name),
                       token_hash=hooks.hash_token(token), status="draft", action=src.action, channel_id=src.channel_id,
                       template_name=src.template_name, template_language=src.template_language, flow_id=src.flow_id,
                       params=list(src.params or []), options=dict(src.options or {}), created_by=agent.id)
    session.add(h)
    await session.commit()
    return await hook_out(session, h, token)


@router.delete("/{hook_id}")
async def delete_hook(hook_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await _get(session, hook_id, agent.organization_id)
    await session.delete(h)
    await session.commit()
    return {"ok": True}


@router.get("/{hook_id}/runs")
async def runs(hook_id: int, limit: int = 50, before: datetime | None = None, status: str | None = None,
               agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Historial de ejecuciones (los valores sensibles van enmascarados)."""
    h = await _get(session, hook_id, agent.organization_id)
    q = select(InboundWebhookRun).where(InboundWebhookRun.webhook_id == h.id,
                                        InboundWebhookRun.organization_id == h.organization_id)
    if before:
        q = q.where(InboundWebhookRun.created_at < before)
    if status:
        q = q.where(InboundWebhookRun.status == status)
    rows = (await session.scalars(q.order_by(InboundWebhookRun.created_at.desc()).limit(min(max(limit, 1), 200)))).all()
    items = [{"id": r.id, "status": r.status, "http_status": r.http_status, "payload": r.payload, "result": r.result,
              "contact_id": r.contact_id, "conversation_id": r.conversation_id, "error": r.error,
              "latency_ms": r.latency_ms, "ip": r.ip, "created_at": r.created_at} for r in rows]
    return {"items": items, "next_before": items[-1]["created_at"] if len(items) == min(max(limit, 1), 200) else None}


class TestIn(BaseModel):
    params: dict | None = None


@router.post("/{hook_id}/test")
async def test(hook_id: int, body: TestIn | None = None, agent: Agent = Depends(current_agent),
               session: AsyncSession = Depends(get_session)):
    """Prueba sin efectos: valida los parámetros (los de ejemplo si no se envían) y muestra el mapeo."""
    h = await _get(session, hook_id, agent.organization_id)
    payload = (body.params if body and body.params else None) or {
        p["name"]: p.get("example") for p in h.params or [] if p.get("example") not in (None, "")}
    try:
        result = await hooks.execute(session, h, payload, dry_run=True)
    except hooks.HookError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message, "details": e.details}}
    curl = (f"curl -X POST '{hook_url(h.slug)}' -H 'Content-Type: application/json' -H 'X-Hook-Token: <TOKEN>' "
            f"-d '{__import__('json').dumps(payload, ensure_ascii=False, default=str)}'")
    return {**result, "payload": payload, "curl": curl}
