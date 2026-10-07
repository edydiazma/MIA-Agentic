import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.automations import SLA_TYPES, TYPES, validate_sla
from app.db import get_session
from app.models import Agent, Automation, OutboundWebhook
from app.realtime import PUBLIC_EVENTS
from app.schemas import UTCDateTime
from app.secrets_vault import delete_secret, get_secret, put_secret
from app.settings_store import add_revision
from app.webhooks_out import deliver, invalidate_cache

router = APIRouter(prefix="/api", tags=["automations"])


class AutomationIn(BaseModel):
    name: str
    type: str
    config: dict = {}
    enabled: bool = True
    priority: int = 100


class AutomationOut(AutomationIn):
    id: int


class WebhookIn(BaseModel):
    name: str
    url: str
    events: list[str] = []
    active: bool = True


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str
    secret: str = ""  # solo se devuelve al crear o con /secret (vive en Vault)
    has_secret: bool = True
    events: list[str]
    active: bool
    consecutive_failures: int
    last_status: int | None
    last_error: str | None
    last_delivery_at: UTCDateTime | None


def _validate(body: AutomationIn) -> None:
    if body.type not in TYPES:
        raise HTTPException(422, f"Tipo inválido: {', '.join(TYPES)}")
    c = body.config
    if body.type in ("keyword_reply", "keyword_handoff") and not c.get("keywords"):
        raise HTTPException(422, "Indica al menos una palabra clave")
    if body.type == "keyword_reply" and not c.get("reply"):
        raise HTTPException(422, "Indica la respuesta")
    if body.type == "welcome" and not c.get("message"):
        raise HTTPException(422, "Indica el mensaje de bienvenida")
    if body.type == "inactivity_close" and not float(c.get("hours") or 0) > 0:
        raise HTTPException(422, "Indica las horas de inactividad")
    if body.type == "business_hours" and not c.get("message"):
        raise HTTPException(422, "Indica el mensaje fuera de horario")
    if body.type in SLA_TYPES and (err := validate_sla(c)):
        raise HTTPException(422, err)


def _to_out(a: Automation) -> AutomationOut:
    return AutomationOut(id=a.id, name=a.name, type=a.type, config=a.config or {}, enabled=a.enabled,
                         priority=a.priority)


async def _automation(session: AsyncSession, aid: int, agent: Agent) -> Automation:
    a = await session.get(Automation, aid)
    if not a or a.organization_id != agent.organization_id:
        raise HTTPException(404, "Automatización no encontrada")
    return a


@router.get("/automations", response_model=list[AutomationOut])
async def list_automations(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Automation).where(Automation.organization_id == agent.organization_id)
                                  .order_by(Automation.priority, Automation.id))).all()
    return [_to_out(a) for a in rows]


@router.post("/automations", response_model=AutomationOut)
async def create_automation(body: AutomationIn, agent: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    _validate(body)
    a = Automation(organization_id=agent.organization_id, **body.model_dump())
    session.add(a)
    await session.flush()
    await add_revision(session, agent.organization_id, "automation", a.id, body.model_dump(), "human", agent.id)
    await session.commit()
    return _to_out(a)


@router.put("/automations/{aid}", response_model=AutomationOut)
async def update_automation(aid: int, body: AutomationIn, agent: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    a = await _automation(session, aid, agent)
    _validate(body)
    for k, v in body.model_dump().items():
        setattr(a, k, v)
    await add_revision(session, agent.organization_id, "automation", a.id, body.model_dump(), "human", agent.id)
    await session.commit()
    return _to_out(a)


@router.delete("/automations/{aid}")
async def delete_automation(aid: int, agent: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    await session.delete(await _automation(session, aid, agent))
    await session.commit()
    return {"ok": True}


# --- Webhooks salientes (secreto de firma en Vault) -----------------------------
def _hook_out(w: OutboundWebhook, secret: str = "") -> WebhookOut:
    return WebhookOut(id=w.id, name=w.name, url=w.url, secret=secret, has_secret=bool(w.signing_secret_id),
                      events=w.events or [], active=w.active, consecutive_failures=w.consecutive_failures,
                      last_status=w.last_status, last_error=w.last_error, last_delivery_at=w.last_delivery_at)


async def _hook(session: AsyncSession, wid: int, agent: Agent) -> OutboundWebhook:
    w = await session.get(OutboundWebhook, wid)
    if not w or w.organization_id != agent.organization_id:
        raise HTTPException(404, "Webhook no encontrado")
    return w


def _check(body: WebhookIn) -> None:
    if not body.url.startswith("https://"):
        raise HTTPException(422, "La URL debe usar https://")
    bad = set(body.events) - PUBLIC_EVENTS
    if bad:
        raise HTTPException(422, f"Eventos inválidos: {', '.join(sorted(bad))}")


@router.get("/webhooks/events")
async def webhook_events(_: Agent = Depends(current_agent)):
    return sorted(PUBLIC_EVENTS)


@router.get("/outbound-webhooks", response_model=list[WebhookOut])
async def list_webhooks(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(OutboundWebhook).where(
        OutboundWebhook.organization_id == agent.organization_id).order_by(OutboundWebhook.id))).all()
    return [_hook_out(w) for w in rows]


@router.post("/outbound-webhooks", response_model=WebhookOut)
async def create_webhook(body: WebhookIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    _check(body)
    w = OutboundWebhook(organization_id=agent.organization_id, **body.model_dump())
    session.add(w)
    await session.flush()
    secret = secrets.token_hex(24)
    w.signing_secret_id = await put_secret(session, secret, f"outbound_webhook:{w.id}")
    await session.commit()
    invalidate_cache()
    return _hook_out(w, secret)  # única vez que se devuelve sin pedirlo


@router.get("/outbound-webhooks/{wid}/secret")
async def reveal_webhook_secret(wid: int, agent: Agent = Depends(require_admin),
                                session: AsyncSession = Depends(get_session)):
    w = await _hook(session, wid, agent)
    return {"secret": await get_secret(session, w.signing_secret_id) or ""}


@router.post("/outbound-webhooks/{wid}/rotate-secret", response_model=WebhookOut)
async def rotate_webhook_secret(wid: int, agent: Agent = Depends(require_admin),
                                session: AsyncSession = Depends(get_session)):
    w = await _hook(session, wid, agent)
    secret = secrets.token_hex(24)
    w.signing_secret_id = await put_secret(session, secret, f"outbound_webhook:{w.id}", w.signing_secret_id)
    await session.commit()
    invalidate_cache()
    return _hook_out(w, secret)


@router.put("/outbound-webhooks/{wid}", response_model=WebhookOut)
async def update_webhook(wid: int, body: WebhookIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    w = await _hook(session, wid, agent)
    _check(body)
    for k, v in body.model_dump().items():
        setattr(w, k, v)
    if body.active:
        w.consecutive_failures = 0
    await session.commit()
    invalidate_cache()
    return _hook_out(w)


@router.delete("/outbound-webhooks/{wid}")
async def delete_webhook(wid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    w = await _hook(session, wid, agent)
    await delete_secret(session, w.signing_secret_id)
    await session.delete(w)
    await session.commit()
    invalidate_cache()
    return {"ok": True}


@router.post("/outbound-webhooks/{wid}/test", response_model=WebhookOut)
async def test_webhook(wid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    w = await _hook(session, wid, agent)
    if not w.active:
        raise HTTPException(409, "El webhook está inactivo")
    invalidate_cache()
    await deliver("webhook.test", {"message": "Evento de prueba"}, only_id=w.id)
    await session.refresh(w)
    return _hook_out(w)
