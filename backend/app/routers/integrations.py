"""Integraciones CRM (HubSpot, Salesforce): conexión OAuth/token, mapeo de campos, sincronización y bitácora.

Nota: GET /api/integrations (catálogo del Centro de Control) vive en routers/config.py; el estado de las
conexiones CRM está en GET /api/integrations/crm.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.config import get_settings
from app.crm import connections as cx
from app.crm import hubspot, salesforce
from app.crm.base import PROVIDERS, CRMError
from app.crm.sync import sync_connection
from app.db import SessionLocal, get_session
from app.models import Agent, InboundEvent, IntegrationConnection, IntegrationMapping, IntegrationOutbox
from app.plans import feature_required
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
log = logging.getLogger(__name__)
CRM = [Depends(feature_required("crm"))]
LOCAL_FIELDS = {"contact": ["name", "first_name", "last_name", "email", "phone", "stage", "notes", "memory"],
                "deal": ["deal.name", "deal.amount", "deal.stage", "deal.status", "deal.currency", "deal.close_date"]}
_tasks: set[asyncio.Task] = set()


class TokenIn(BaseModel):
    token: str


class MappingIn(BaseModel):
    object: str
    local_field: str
    remote_property: str
    direction: str = "both"
    transform: dict | None = None


class MappingsIn(BaseModel):
    mappings: list[MappingIn]


class ConnSettingsIn(BaseModel):
    sync_enabled: bool | None = None
    contact_object: str | None = None  # Salesforce: Contact | Lead
    push_contacts: str | None = None  # all | with_deal | none
    push_notes: bool | None = None


class OutboxOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    operation: str
    status: str
    attempts: int
    error: str | None
    next_attempt_at: UTCDateTime
    created_at: UTCDateTime
    sent_at: UTCDateTime | None


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(404, "Proveedor no soportado")
    return provider


async def _conn(session: AsyncSession, org: int, provider: str) -> IntegrationConnection:
    conn = await cx.get_connection(session, org, _provider(provider))
    if not conn:
        raise HTTPException(404, f"{PROVIDERS[provider]} no está conectado")
    return conn


async def _status(session: AsyncSession, org: int, provider: str) -> dict:
    conn = await cx.get_connection(session, org, provider)
    out = {"provider": provider, "label": PROVIDERS[provider], "configured": cx.configured(provider),
           "connected": bool(conn), "status": conn.status if conn else None}
    if conn:
        counts = dict((await session.execute(select(IntegrationOutbox.status, func.count()).where(
            IntegrationOutbox.connection_id == conn.id).group_by(IntegrationOutbox.status))).all())
        s = conn.settings or {}
        out.update({
            "id": conn.id, "external_account_id": conn.external_account_id, "instance_url": conn.instance_url,
            "last_sync_at": conn.last_sync_at, "last_error": conn.last_error, "sync_enabled": conn.sync_enabled,
            "settings": {k: s.get(k) for k in ("contact_object", "push_contacts", "push_notes", "last_pull_at")},
            "via_oauth": bool(conn.refresh_token_secret_id), "connected_at": conn.created_at,
            "outbox": {"pending": counts.get("pending", 0), "sent": counts.get("sent", 0),
                       "skipped": counts.get("skipped", 0), "failed": counts.get("failed", 0)},
        })
    return out


@router.get("/crm")
async def crm_status(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return [await _status(session, agent.organization_id, p) for p in PROVIDERS]


# --- Conexión -------------------------------------------------------------------
@router.get("/{provider}/connect", dependencies=CRM)
async def connect(provider: str, agent: Agent = Depends(require_admin)):
    _provider(provider)
    if not cx.configured(provider):
        raise HTTPException(409, f"Falta configurar la app de {PROVIDERS[provider]} en el servidor "
                                 f"({provider.upper()}_CLIENT_ID / _CLIENT_SECRET)")
    return {"url": cx.authorize_url(provider, agent.organization_id, agent.id)}


def _front(params: dict) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/configuraciones/integraciones?{urlencode(params)}", status_code=302)


@router.get("/{provider}/callback")
async def callback(provider: str, code: str | None = None, state: str | None = None, error: str | None = None,
                   session: AsyncSession = Depends(get_session)):
    """Regreso del OAuth (sin sesión del panel: la organización viaja en el `state` firmado)."""
    _provider(provider)
    if error or not code or not state:
        return _front({"provider": provider, "error": error or "Autorización cancelada"})
    try:
        st = cx.read_state(state, provider)
    except jwt.PyJWTError:
        return _front({"provider": provider, "error": "El enlace de autorización venció; intenta de nuevo"})
    s = get_settings()
    try:
        if provider == "hubspot":
            tokens = await hubspot.exchange_code(s.hubspot_client_id, s.hubspot_client_secret,
                                                 cx.redirect_uri(provider), code)
            account = await hubspot.HubSpotAdapter(tokens["access_token"]).account()
            await cx.upsert_connection(session, st["org"], provider, st["agent"], tokens,
                                       str(account.get("portalId") or ""), scopes=hubspot.SCOPES)
        else:
            tokens = await salesforce.exchange_code(s.salesforce_login_url, s.salesforce_client_id,
                                                    s.salesforce_client_secret, cx.redirect_uri(provider), code)
            org_id = (tokens.get("id") or "").rstrip("/").split("/")[-2:-1]
            await cx.upsert_connection(session, st["org"], provider, st["agent"], tokens,
                                       org_id[0] if org_id else None, instance_url=tokens.get("instance_url"),
                                       scopes=salesforce.SCOPES)
    except CRMError as e:
        log.warning("OAuth %s falló: %s", provider, e)
        return _front({"provider": provider, "error": "No se pudo completar la conexión"})
    return _front({"provider": provider, "connected": "1"})


@router.post("/hubspot/token", dependencies=CRM)
async def hubspot_token(body: TokenIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    """Conexión con token de app privada de HubSpot (sin OAuth)."""
    token = body.token.strip()
    if not token:
        raise HTTPException(422, "Pega el token de la app privada")
    try:
        account = await hubspot.HubSpotAdapter(token).account()
    except CRMError as e:
        raise HTTPException(422, f"HubSpot rechazó el token: {e}") from None
    await cx.upsert_connection(session, agent.organization_id, "hubspot", agent.id, {"access_token": token},
                               str(account.get("portalId") or ""))
    return await _status(session, agent.organization_id, "hubspot")


@router.delete("/{provider}")
async def disconnect(provider: str, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    await cx.disconnect(session, conn)
    return {"ok": True}


# --- Configuración y mapeo ------------------------------------------------------
@router.get("/{provider}/mappings")
async def get_mappings(provider: str, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    rows = (await session.scalars(select(IntegrationMapping).where(IntegrationMapping.connection_id == conn.id)
                                  .order_by(IntegrationMapping.object, IntegrationMapping.id))).all()
    from app.fields import fields_by_key

    customs = [f"custom:{k}" for k in await fields_by_key(session, agent.organization_id)]
    return {"mappings": [{"id": m.id, "object": m.object, "local_field": m.local_field,
                          "remote_property": m.remote_property, "direction": m.direction, "transform": m.transform}
                         for m in rows],
            "local_fields": {"contact": LOCAL_FIELDS["contact"] + customs, "deal": LOCAL_FIELDS["deal"]},
            "defaults": cx.default_mappings(provider, (conn.settings or {}).get("contact_object", "Contact"))}


@router.put("/{provider}/mappings", dependencies=CRM)
async def put_mappings(provider: str, body: MappingsIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    seen = set()
    from app.fields import fields_by_key

    customs = {f"custom:{k}" for k in await fields_by_key(session, agent.organization_id)}
    for m in body.mappings:
        if m.object not in LOCAL_FIELDS or m.direction not in ("push", "pull", "both"):
            raise HTTPException(422, f"Mapeo inválido: {m.local_field}")
        if m.local_field not in LOCAL_FIELDS[m.object] and m.local_field not in customs:
            raise HTTPException(422, f"Campo local desconocido: {m.local_field}")
        if not m.remote_property.strip():
            raise HTTPException(422, f"Falta la propiedad del CRM para {m.local_field}")
        if (m.object, m.local_field) in seen:
            raise HTTPException(422, f"Campo repetido: {m.local_field}")
        seen.add((m.object, m.local_field))
    await session.execute(delete(IntegrationMapping).where(IntegrationMapping.connection_id == conn.id))
    for m in body.mappings:
        session.add(IntegrationMapping(connection_id=conn.id, object=m.object, local_field=m.local_field,
                                       remote_property=m.remote_property.strip(), direction=m.direction,
                                       transform=m.transform))
    await session.commit()
    return await get_mappings(provider, agent, session)


@router.put("/{provider}/settings", dependencies=CRM)
async def put_settings(provider: str, body: ConnSettingsIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    s = dict(conn.settings or {})
    if body.contact_object is not None:
        if body.contact_object not in ("Contact", "Lead"):
            raise HTTPException(422, "Objeto inválido (Contact o Lead)")
        s["contact_object"] = body.contact_object
    if body.push_contacts is not None:
        if body.push_contacts not in ("all", "with_deal", "none"):
            raise HTTPException(422, "Opción inválida")
        s["push_contacts"] = body.push_contacts
    if body.push_notes is not None:
        s["push_notes"] = body.push_notes
    conn.settings = s
    if body.sync_enabled is not None:
        conn.sync_enabled = body.sync_enabled
    await session.commit()
    return await _status(session, agent.organization_id, provider)


# --- Sincronización -------------------------------------------------------------
@router.post("/{provider}/sync", dependencies=CRM)
async def sync_now(provider: str, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    conn = await _conn(session, org, provider)
    if conn.status != "connected":
        raise HTTPException(409, "La conexión tiene un error: vuelve a conectar la cuenta")
    result = await sync_connection(conn.id, force_pull=True)
    session.expire_all()
    return {"result": result, "status": await _status(session, org, provider)}


@router.get("/{provider}/outbox", response_model=list[OutboxOut])
async def outbox(provider: str, status: str | None = None, limit: int = Query(default=100, le=500),
                 agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    stmt = select(IntegrationOutbox).where(IntegrationOutbox.connection_id == conn.id)
    if status:
        stmt = stmt.where(IntegrationOutbox.status == status)
    rows = (await session.scalars(stmt.order_by(IntegrationOutbox.created_at.desc()).limit(limit))).all()
    return [OutboxOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/{provider}/outbox/{row_id}/retry", dependencies=CRM)
async def retry(provider: str, row_id: int, agent: Agent = Depends(require_admin),
                session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, provider)
    row = (await session.scalars(select(IntegrationOutbox).where(
        IntegrationOutbox.id == row_id, IntegrationOutbox.connection_id == conn.id))).first()
    if not row:
        raise HTTPException(404, "Registro no encontrado")
    from app.models import utcnow

    row.status, row.attempts, row.next_attempt_at, row.error = "pending", 0, utcnow(), None
    await session.commit()
    return {"ok": True}


# --- Webhook de HubSpot (opcional) ----------------------------------------------
def verify_hubspot_signature(secret: str, method: str, uri: str, body: bytes, timestamp: str, signature: str) -> bool:
    try:
        if abs(time.time() * 1000 - int(timestamp)) > 5 * 60 * 1000:
            return False
    except ValueError:
        return False
    raw = method.encode() + uri.encode() + body + timestamp.encode()
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


@router.post("/hubspot/webhook")
async def hubspot_webhook(request: Request):
    s = get_settings()
    body = await request.body()
    uri = f"{s.public_base_url.rstrip('/')}{request.url.path}"
    if not verify_hubspot_signature(s.hubspot_client_secret, "POST", uri, body,
                                    request.headers.get("X-HubSpot-Request-Timestamp", ""),
                                    request.headers.get("X-HubSpot-Signature-v3", "")):
        raise HTTPException(401, "Firma inválida")
    try:
        events = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "JSON inválido") from None
    portals = {str(e.get("portalId")) for e in events if isinstance(e, dict)}
    async with SessionLocal() as session:
        conns = (await session.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider == "hubspot", IntegrationConnection.external_account_id.in_(portals)))).all()
        for conn in conns:
            session.add(InboundEvent(organization_id=conn.organization_id, source="hubspot",
                                     payload={"events": [e for e in events if str(e.get("portalId")) ==
                                                         conn.external_account_id]}))
        await session.commit()
        ids = [c.id for c in conns]
    for conn_id in ids:  # trae los cambios en segundo plano; HubSpot espera respuesta rápida
        task = asyncio.create_task(sync_connection(conn_id, force_pull=True))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"ok": True}
