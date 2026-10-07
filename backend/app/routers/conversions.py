"""Conversiones: acciones (qué cuenta y a dónde va), eventos registrados y log de envíos con reintento."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.conversions import process_upload, scan_org
from app.db import get_session
from app.models import Agent, Contact, ConversionAction, ConversionEvent, ConversionUpload, Typification, utcnow
from app.plans import feature_required
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api", tags=["conversions"], dependencies=[Depends(feature_required("attribution"))])
TRIGGERS = ("typification", "stage_client", "appointment_booked", "deal_won")
META_EVENTS = ("Purchase", "Lead", "CompleteRegistration", "Schedule", "Contact", "SubmitApplication", "QualifiedLead")


class GoogleAdsDest(BaseModel):
    customer_id: str
    conversion_action_id: str


class MetaDest(BaseModel):
    dataset_id: str
    event_name: str = "Purchase"
    whatsapp_business_account_id: str | None = None


class ActionIn(BaseModel):
    name: str
    trigger: str
    typification_id: int | None = None
    value: float | None = None
    currency: str = "COP"
    google_ads: GoogleAdsDest | None = None
    meta: MetaDest | None = None
    is_active: bool = True


class ActionOut(BaseModel):
    id: int
    name: str
    trigger: str
    typification_id: int | None
    typification: str | None = None
    value: float | None
    currency: str
    google_ads: dict | None
    meta: dict | None
    is_active: bool
    events_30d: int = 0


async def _validate(session: AsyncSession, body: ActionIn, org: int) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")
    if body.trigger not in TRIGGERS:
        raise HTTPException(422, f"Disparador inválido: {', '.join(TRIGGERS)}")
    if body.trigger == "typification":
        t = await session.get(Typification, body.typification_id) if body.typification_id else None
        if not t or t.organization_id != org:
            raise HTTPException(422, "Elige la tipificación que cuenta como conversión")
    if not (body.google_ads or body.meta):
        raise HTTPException(422, "Configura al menos un destino (Google Ads o Meta)")
    if body.meta and body.meta.event_name not in META_EVENTS:
        raise HTTPException(422, f"Evento de Meta inválido: {', '.join(META_EVENTS)}")
    if body.value is not None and body.value < 0:
        raise HTTPException(422, "El valor no puede ser negativo")
    data = body.model_dump()
    data["name"] = body.name.strip()
    data["currency"] = body.currency.upper()[:3]
    data["typification_id"] = body.typification_id if body.trigger == "typification" else None
    if body.google_ads:
        data["google_ads"] = {k: v.replace("-", "").strip() for k, v in body.google_ads.model_dump().items()}
    if body.meta:
        data["meta"] = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.meta.model_dump().items()}
    return data


async def _out(session: AsyncSession, a: ConversionAction) -> ActionOut:
    typ = await session.get(Typification, a.typification_id) if a.typification_id else None
    n = await session.scalar(select(func.count()).select_from(ConversionEvent).where(
        ConversionEvent.action_id == a.id, ConversionEvent.occurred_at >= utcnow() - timedelta(days=30)))
    return ActionOut(id=a.id, name=a.name, trigger=a.trigger, typification_id=a.typification_id,
                     typification=typ.name if typ else None, value=float(a.value) if a.value is not None else None,
                     currency=a.currency, google_ads=a.google_ads, meta=a.meta, is_active=a.is_active, events_30d=n or 0)


async def _action(session: AsyncSession, action_id: int, org: int) -> ConversionAction:
    a = await session.get(ConversionAction, action_id)
    if not a or a.organization_id != org:
        raise HTTPException(404, "Acción no encontrada")
    return a


@router.get("/conversion-actions/options", response_model=dict)
async def action_options(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Opciones para el formulario: tipificaciones (con id), disparadores y eventos de Meta."""
    typs = (await session.execute(select(Typification.id, Typification.name).where(
        Typification.organization_id == agent.organization_id).order_by(Typification.name))).all()
    return {"typifications": [{"id": i, "name": n} for i, n in typs], "triggers": list(TRIGGERS),
            "meta_events": list(META_EVENTS)}


@router.get("/conversion-actions", response_model=list[ActionOut])
async def list_actions(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(ConversionAction).where(
        ConversionAction.organization_id == agent.organization_id).order_by(ConversionAction.id))).all()
    return [await _out(session, a) for a in rows]


@router.post("/conversion-actions", response_model=ActionOut)
async def create_action(body: ActionIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    data = await _validate(session, body, agent.organization_id)
    if await session.scalar(select(ConversionAction.id).where(
            ConversionAction.organization_id == agent.organization_id, ConversionAction.name == data["name"])):
        raise HTTPException(409, "Ya existe una acción con ese nombre")
    a = ConversionAction(organization_id=agent.organization_id, **data)
    session.add(a)
    await session.commit()
    return await _out(session, a)


@router.put("/conversion-actions/{action_id}", response_model=ActionOut)
async def update_action(action_id: int, body: ActionIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    a = await _action(session, action_id, agent.organization_id)
    for k, v in (await _validate(session, body, agent.organization_id)).items():
        setattr(a, k, v)
    await session.commit()
    return await _out(session, a)


@router.delete("/conversion-actions/{action_id}")
async def delete_action(action_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Desactiva (los eventos y envíos históricos se conservan)."""
    a = await _action(session, action_id, agent.organization_id)
    a.is_active = False
    await session.commit()
    return {"ok": True}


class UploadOut(BaseModel):
    id: int
    event_id: int
    destination: str
    status: str
    attempts: int
    error: str | None
    next_attempt_at: UTCDateTime
    sent_at: UTCDateTime | None
    action: str
    contact: str
    conversation_id: int | None
    value: float | None
    currency: str
    occurred_at: UTCDateTime
    channel: str | None


@router.get("/conversion-uploads", response_model=list[UploadOut])
async def list_uploads(status: str | None = None, destination: str | None = None,
                       limit: int = Query(default=100, le=500), agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    stmt = (select(ConversionUpload).join(ConversionEvent, ConversionEvent.id == ConversionUpload.event_id)
            .where(ConversionEvent.organization_id == agent.organization_id)
            .order_by(ConversionUpload.id.desc()).limit(limit))
    if status:
        stmt = stmt.where(ConversionUpload.status == status)
    if destination:
        stmt = stmt.where(ConversionUpload.destination == destination)
    ups = (await session.scalars(stmt)).unique().all()
    contacts = {c.id: c for c in (await session.scalars(select(Contact).where(
        Contact.id.in_({u.event.contact_id for u in ups} or {0})))).all()}
    out = []
    for u in ups:
        ev = u.event
        c = contacts.get(ev.contact_id)
        out.append(UploadOut(id=u.id, event_id=u.event_id, destination=u.destination, status=u.status,
                             attempts=u.attempts, error=u.error, next_attempt_at=u.next_attempt_at, sent_at=u.sent_at,
                             action=ev.action.name, contact=(c.name or f"+{c.wa_id}") if c else "—",
                             conversation_id=ev.conversation_id, value=float(ev.value) if ev.value is not None else None,
                             currency=ev.currency, occurred_at=ev.occurred_at,
                             channel=(ev.attribution or {}).get("channel")))
    return out


@router.post("/conversion-uploads/{upload_id}/retry", response_model=dict)
async def retry_upload(upload_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    up = await session.get(ConversionUpload, upload_id)
    if not up or up.event.organization_id != agent.organization_id:
        raise HTTPException(404, "Envío no encontrado")
    if up.status == "sent":
        raise HTTPException(409, "Ya se envió")
    if up.status == "skipped":
        raise HTTPException(409, f"No se puede enviar: {up.error}")
    up.status, up.attempts = "pending", 0
    await process_upload(session, up)
    return {"id": up.id, "status": up.status, "error": up.error, "attempts": up.attempts}


@router.post("/conversion-actions/scan", response_model=dict)
async def scan_now(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Detecta conversiones nuevas ahora (sin esperar el ciclo de 60 s)."""
    return {"created": await scan_org(session, agent.organization_id)}
