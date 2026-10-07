"""Hub de integraciones (§21.3): tiendas (Shopify, WooCommerce, VTEX), calendarios (Google, Outlook), conectores
propios (REST + mapeo) y exportación de datos (BigQuery, S3, GCS, descarga).

Zoho CRM y Odoo viven en /api/integrations (marco CRM: outbox, mapeos y sync). Webhooks públicos de tiendas y
conectores: POST /webhooks/hub/{provider}/{connection_id}.
"""

import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs, storage
from app.auth import current_agent
from app.config import get_settings
from app.crm import connections as cx
from app.db import get_session
from app.hub import builder, calendars, commerce, common, exports, http
from app.hub.http import HubError
from app.models import (
    Agent,
    ConnectorDefinition,
    ConnectorRun,
    DataExport,
    DataExportRun,
    ExternalOrder,
    IntegrationConnection,
    utcnow,
)
from app.permissions import has_permission, require_permission
from app.secrets_vault import delete_secret

router = APIRouter(tags=["hub"])
log = logging.getLogger(__name__)
MANAGE = require_permission("integrations.manage")
EXPORTS = require_permission("exports.data")
STORE_SECRETS = {"shopify": ("access_token", "webhook_secret"),
                 "woocommerce": ("consumer_key", "consumer_secret", "webhook_secret"),
                 "vtex": ("app_key", "app_token", "webhook_secret")}
SETTING_KEYS = ("sync_minutes", "sync_catalog", "attribution_days", "create_contacts", "country", "push_appointments",
                "busy_blocks_slots", "calendars", "crm_push", "push_contacts", "push_notes", "initial_days")
SHOPIFY_SCOPES = "read_products,read_orders,read_customers,read_inventory"


def webhook_url(provider: str, conn_id: int) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/webhooks/hub/{provider}/{conn_id}"


def _front(params: dict) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/configuraciones/integraciones?{urlencode(params)}", status_code=302)


async def _own(session: AsyncSession, agent: Agent, conn_id: int,
               providers: tuple[str, ...] | None = None) -> IntegrationConnection:
    conn = await common.connection(session, agent.organization_id, conn_id, providers)
    if not conn:
        raise HTTPException(404, "Conexión no encontrada")
    return conn


async def conn_json(session: AsyncSession, conn: IntegrationConnection) -> dict:
    s = conn.settings or {}
    last = await session.scalar(select(ConnectorRun).where(ConnectorRun.connection_id == conn.id)
                                .order_by(ConnectorRun.started_at.desc()).limit(1))
    orders = await session.scalar(select(func.count()).select_from(ExternalOrder).where(
        ExternalOrder.connection_id == conn.id)) if conn.provider in (*common.COMMERCE, "custom") else None
    return {"id": conn.id, "provider": conn.provider, "provider_label": common.LABELS.get(conn.provider, conn.provider),
            "label": conn.label, "status": conn.status, "last_error": conn.last_error,
            "external_account_id": conn.external_account_id, "instance_url": conn.instance_url,
            "connector_id": conn.connector_id, "sync_enabled": conn.sync_enabled, "last_sync_at": conn.last_sync_at,
            "settings": {k: s.get(k) for k in (*SETTING_KEYS, "shop", "url", "account", "environment") if k in s},
            "secrets_set": sorted((s.get("secret_ids") or {}).keys()) +
                           (["access_token"] if conn.access_token_secret_id else []),
            "webhook_url": webhook_url(conn.provider, conn.id) if conn.provider in (*common.COMMERCE, "custom") else None,
            "orders": orders, "created_at": conn.created_at,
            "last_run": _run_json(last) if last else None}


def _run_json(r: ConnectorRun) -> dict:
    return {"id": r.id, "entity": r.entity, "direction": r.direction, "status": r.status, "fetched": r.fetched,
            "created": r.created, "updated": r.updated, "skipped": r.skipped, "failed": r.failed, "error": r.error,
            "sample_errors": r.sample_errors or [], "started_at": r.started_at, "finished_at": r.finished_at}


async def _drop(session: AsyncSession, conn: IntegrationConnection) -> None:
    for sid in ((conn.settings or {}).get("secret_ids") or {}).values():
        await delete_secret(session, sid)
    await delete_secret(session, conn.access_token_secret_id)
    await delete_secret(session, conn.refresh_token_secret_id)
    await session.delete(conn)
    await session.commit()


async def _kick(session: AsyncSession, conn: IntegrationConnection, full: bool = False) -> dict:
    """Sincroniza ahora: por la cola si está activa; si no, en línea."""
    if jobs.enabled():
        await jobs.enqueue(session, "hub.sync_connection", {"connection_id": conn.id, "full": full}, queue="crm",
                           organization_id=conn.organization_id, dedupe_key=f"hub:{conn.id}")
        await session.commit()
        return {"queued": True}
    from app.hub.runner import sync_connection

    return {"queued": False, "result": await sync_connection(conn.id, full=full)}


# --- Catálogo y conexiones -------------------------------------------------------------------------------------
@router.get("/api/hub/providers")
async def providers(agent: Agent = Depends(current_agent)):
    s = get_settings()
    return [
        {"provider": "shopify", "label": "Shopify", "kind": "commerce", "multiple": True,
         "oauth": bool(s.shopify_client_id and s.shopify_client_secret), "fields": ["shop", "access_token", "webhook_secret"]},
        {"provider": "woocommerce", "label": "WooCommerce", "kind": "commerce", "multiple": True, "oauth": False,
         "fields": ["url", "consumer_key", "consumer_secret", "webhook_secret"]},
        {"provider": "vtex", "label": "VTEX", "kind": "commerce", "multiple": True, "oauth": False,
         "fields": ["account", "environment", "app_key", "app_token"]},
        {"provider": "google_calendar", "label": "Google Calendar", "kind": "calendar", "multiple": False,
         "oauth": calendars.configured("google_calendar")},
        {"provider": "microsoft_calendar", "label": "Outlook / Microsoft 365", "kind": "calendar", "multiple": False,
         "oauth": calendars.configured("microsoft_calendar")},
        {"provider": "zoho", "label": "Zoho CRM", "kind": "crm", "multiple": False, "oauth": cx.configured("zoho"),
         "path": "/api/integrations/zoho/connect"},
        {"provider": "odoo", "label": "Odoo", "kind": "crm", "multiple": False, "oauth": False,
         "fields": ["url", "db", "login", "api_key"], "path": "/api/integrations/odoo/credentials"},
        {"provider": "custom", "label": "Conector propio", "kind": "custom", "multiple": True, "oauth": False},
    ]


@router.get("/api/hub/connections")
async def list_connections(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == agent.organization_id,
        IntegrationConnection.provider.in_((*common.COMMERCE, *common.CALENDARS, "custom")))
        .order_by(IntegrationConnection.provider, IntegrationConnection.id))).all()
    return [await conn_json(session, c) for c in rows]


class StoreIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    shop: str | None = None  # Shopify: tienda.myshopify.com
    url: str | None = None  # WooCommerce: https://tienda.com
    account: str | None = None  # VTEX
    environment: str | None = None  # VTEX: vtexcommercestable
    access_token: str | None = None
    consumer_key: str | None = None
    consumer_secret: str | None = None
    app_key: str | None = None
    app_token: str | None = None
    webhook_secret: str | None = None
    attribution_days: int = 30
    sync_catalog: bool = True


async def _label_free(session: AsyncSession, org: int, provider: str, label: str, exclude: int | None = None) -> None:
    q = select(IntegrationConnection.id).where(IntegrationConnection.organization_id == org,
                                               IntegrationConnection.provider == provider,
                                               func.lower(IntegrationConnection.label) == label.strip().lower())
    if exclude:
        q = q.where(IntegrationConnection.id != exclude)
    if await session.scalar(q):
        raise HTTPException(409, f"Ya hay una conexión «{label}» de {common.LABELS.get(provider, provider)}")


@router.post("/api/hub/commerce/{provider}")
async def connect_store(provider: str, body: StoreIn, agent: Agent = Depends(MANAGE),
                        session: AsyncSession = Depends(get_session)):
    """Conecta una tienda con credenciales (token de app personalizada de Shopify, claves REST de WooCommerce o
    appKey/appToken de VTEX). Se valida contra la API antes de guardar. Varias tiendas por proveedor (con etiqueta)."""
    if provider not in common.COMMERCE:
        raise HTTPException(404, "Proveedor no soportado")
    org = agent.organization_id
    await _label_free(session, org, provider, body.label)
    settings = {"attribution_days": max(1, min(body.attribution_days, 180)), "sync_catalog": body.sync_catalog}
    try:
        if provider == "shopify":
            if not (body.shop and body.access_token):
                raise HTTPException(422, "shop y access_token son obligatorios")
            client = commerce.Shopify(body.shop, body.access_token)
            info = await client.shop_info()
            settings["shop"], account, instance = client.shop, client.shop, f"https://{client.shop}"
            settings["currency"] = info.get("currencyCode")
        elif provider == "woocommerce":
            if not (body.url and body.consumer_key and body.consumer_secret):
                raise HTTPException(422, "url, consumer_key y consumer_secret son obligatorios")
            client = commerce.WooCommerce(body.url, body.consumer_key, body.consumer_secret)
            await client.ping()
            settings["url"], account, instance = client.base.split("/wp-json")[0], None, body.url.rstrip("/")
        else:
            if not (body.account and body.app_key and body.app_token):
                raise HTTPException(422, "account, app_key y app_token son obligatorios")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", body.account.strip()) or (
                    body.environment or "vtexcommercestable") not in ("vtexcommercestable", "vtexcommercebeta"):
                raise HTTPException(422, "Cuenta o ambiente de VTEX inválido")
            client = commerce.Vtex(body.account.strip(), body.app_key, body.app_token,
                                   body.environment or "vtexcommercestable")
            await client.ping()
            settings["account"], settings["environment"] = body.account.strip(), body.environment or "vtexcommercestable"
            account, instance = body.account.strip(), None
    except HubError as e:
        raise HTTPException(400, f"No se pudo validar la tienda: {e}") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    conn = IntegrationConnection(organization_id=org, provider=provider, label=body.label.strip(), status="connected",
                                 external_account_id=account, instance_url=instance, connected_by=agent.id,
                                 settings=settings, scopes=[])
    session.add(conn)
    await session.flush()
    for name in STORE_SECRETS[provider]:
        value = getattr(body, name, None)
        if name == "access_token" and value:
            conn.access_token_secret_id = (await cx_put(session, conn, value))
        elif value:
            await common.set_secret(session, conn, name, value)
    await session.commit()
    await _kick(session, conn)
    return await conn_json(session, conn)


async def cx_put(session: AsyncSession, conn: IntegrationConnection, token: str) -> str:
    from app.secrets_vault import put_secret

    return await put_secret(session, token, f"integration:{conn.id}:access", conn.access_token_secret_id)


class SettingsIn(BaseModel):
    label: str | None = None
    sync_enabled: bool | None = None
    settings: dict | None = None
    secrets: dict[str, str] | None = None


@router.put("/api/hub/connections/{conn_id}")
async def update_connection(conn_id: int, body: SettingsIn, agent: Agent = Depends(MANAGE),
                            session: AsyncSession = Depends(get_session)):
    conn = await _own(session, agent, conn_id)
    if body.label is not None and body.label.strip() != (conn.label or ""):
        await _label_free(session, conn.organization_id, conn.provider, body.label, exclude=conn.id)
        conn.label = body.label.strip()[:80]
    if body.sync_enabled is not None:
        conn.sync_enabled = body.sync_enabled
    if body.settings:
        s = dict(conn.settings or {})
        for k, v in body.settings.items():
            if k in SETTING_KEYS:
                s[k] = v
        conn.settings = s
    allowed = set(STORE_SECRETS.get(conn.provider, ())) | (set(builder.SECRET_NAMES) if conn.provider == "custom" else set())
    for name, value in (body.secrets or {}).items():
        if name not in allowed:
            raise HTTPException(422, f"Secreto desconocido: {name}")
        if name == "access_token":
            conn.access_token_secret_id = await cx_put(session, conn, value)
        else:
            await common.set_secret(session, conn, name, value)
    if conn.status == "error" and body.secrets:
        conn.status, conn.last_error = "connected", None
    conn.updated_at = utcnow()
    await session.commit()
    return await conn_json(session, conn)


@router.delete("/api/hub/connections/{conn_id}", status_code=204)
async def delete_connection(conn_id: int, agent: Agent = Depends(MANAGE), session: AsyncSession = Depends(get_session)):
    conn = await _own(session, agent, conn_id, (*common.COMMERCE, *common.CALENDARS, "custom"))
    await _drop(session, conn)


@router.post("/api/hub/connections/{conn_id}/sync")
async def sync_now(conn_id: int, full: bool = False, agent: Agent = Depends(MANAGE),
                   session: AsyncSession = Depends(get_session)):
    conn = await _own(session, agent, conn_id, (*common.COMMERCE, *common.CALENDARS, "custom"))
    return await _kick(session, conn, full=full)


@router.get("/api/hub/connections/{conn_id}/runs")
async def connection_runs(conn_id: int, limit: int = Query(30, le=200), agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    conn = await _own(session, agent, conn_id)
    rows = (await session.scalars(select(ConnectorRun).where(ConnectorRun.connection_id == conn.id)
                                  .order_by(ConnectorRun.started_at.desc()).limit(limit))).all()
    return [_run_json(r) for r in rows]


@router.get("/api/hub/orders")
async def orders(contact_id: int | None = None, connection_id: int | None = None, limit: int = Query(50, le=500),
                 agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Pedidos de tiendas/conectores (ficha del cliente: ?contact_id=)."""
    q = select(ExternalOrder, IntegrationConnection.label, IntegrationConnection.provider).join(
        IntegrationConnection, IntegrationConnection.id == ExternalOrder.connection_id).where(
        ExternalOrder.organization_id == agent.organization_id)
    if contact_id:
        q = q.where(ExternalOrder.contact_id == contact_id)
    if connection_id:
        q = q.where(ExternalOrder.connection_id == connection_id)
    rows = (await session.execute(q.order_by(ExternalOrder.placed_at.desc().nullslast()).limit(limit))).all()
    return [{"id": o.id, "connection_id": o.connection_id, "store": label or common.LABELS.get(p, p), "provider": p,
             "external_id": o.external_id, "order_number": o.order_number, "status": o.status,
             "status_raw": o.status_raw, "total": float(o.total) if o.total is not None else None,
             "currency": o.currency, "items": o.items or [], "contact_id": o.contact_id,
             "attribution_id": o.attribution_id, "placed_at": o.placed_at} for o, label, p in rows]


# --- OAuth: Shopify (app pública) y calendarios -------------------------------------------------------------------
@router.get("/api/hub/oauth/{provider}/start")
async def oauth_start(provider: str, shop: str | None = None, label: str | None = None, agent: Agent = Depends(MANAGE)):
    s = get_settings()
    if provider == "shopify":
        if not (s.shopify_client_id and s.shopify_client_secret):
            raise HTTPException(409, "Falta SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET en el servidor")
        if not shop:
            raise HTTPException(422, "Indica la tienda (tienda.myshopify.com)")
        domain = commerce.Shopify(shop, "").shop
        state = jwt.encode({"org": agent.organization_id, "agent": agent.id, "provider": provider, "shop": domain,
                            "label": (label or domain)[:80], "aud": cx.STATE_AUDIENCE,
                            "exp": utcnow() + timedelta(minutes=10)}, s.jwt_secret, algorithm="HS256")
        return {"url": f"https://{domain}/admin/oauth/authorize?" + urlencode({
            "client_id": s.shopify_client_id, "scope": SHOPIFY_SCOPES, "state": state,
            "redirect_uri": f"{s.public_base_url.rstrip('/')}/api/hub/oauth/shopify/callback"})}
    if provider not in common.CALENDARS:
        raise HTTPException(404, "Proveedor no soportado")
    if not calendars.configured(provider):
        raise HTTPException(409, "Falta configurar la app OAuth del calendario en el servidor")
    return {"url": calendars.authorize_url(provider, cx.make_state(agent.organization_id, agent.id, provider))}


def verify_shopify_query(params: dict, secret: str) -> bool:
    given = params.get("hmac") or ""
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
    return bool(given) and hmac.compare_digest(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest(), given)


@router.get("/api/hub/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, code: str | None = None, state: str | None = None,
                         error: str | None = None, session: AsyncSession = Depends(get_session)):
    if error or not code or not state:
        return _front({"provider": provider, "error": error or "Autorización cancelada"})
    try:
        st = cx.read_state(state, provider)
    except jwt.PyJWTError:
        return _front({"provider": provider, "error": "El enlace de autorización venció; intenta de nuevo"})
    s = get_settings()
    try:
        if provider == "shopify":
            params = dict(request.query_params)
            if not verify_shopify_query(params, s.shopify_client_secret) or params.get("shop") != st.get("shop"):
                return _front({"provider": provider, "error": "Firma de Shopify inválida"})
            tokens = http.check(await http.send("POST", f"https://{st['shop']}/admin/oauth/access_token", json_body={
                "client_id": s.shopify_client_id, "client_secret": s.shopify_client_secret, "code": code}),
                "Shopify").json()
            conn = await session.scalar(select(IntegrationConnection).where(
                IntegrationConnection.organization_id == st["org"], IntegrationConnection.provider == "shopify",
                IntegrationConnection.external_account_id == st["shop"]))
            if not conn:
                await _label_free(session, st["org"], "shopify", st["label"])
                conn = IntegrationConnection(organization_id=st["org"], provider="shopify", label=st["label"],
                                             external_account_id=st["shop"], instance_url=f"https://{st['shop']}",
                                             connected_by=st["agent"], settings={"shop": st["shop"], "oauth": True,
                                                                                 "attribution_days": 30},
                                             scopes=SHOPIFY_SCOPES.split(","))
                session.add(conn)
                await session.flush()
            conn.status, conn.last_error = "connected", None
            conn.access_token_secret_id = await cx_put(session, conn, tokens["access_token"])
        elif provider in common.CALENDARS:
            tokens = await calendars.exchange_code(provider, code)
            conn = await session.scalar(select(IntegrationConnection).where(
                IntegrationConnection.organization_id == st["org"], IntegrationConnection.provider == provider))
            if not conn:
                conn = IntegrationConnection(organization_id=st["org"], provider=provider, connected_by=st["agent"],
                                             settings={"calendars": {"default": "primary", "agents": {}},
                                                       "push_appointments": True, "busy_blocks_slots": True},
                                             scopes=list(calendars.GOOGLE_SCOPES if provider == "google_calendar"
                                                         else calendars.MS_SCOPES))
                session.add(conn)
                await session.flush()
            conn.status, conn.last_error = "connected", None
            await calendars.save_tokens(session, conn, tokens)
        else:
            return _front({"provider": provider, "error": "Proveedor no soportado"})
        await session.commit()
    except (HubError, HTTPException) as e:
        log.warning("OAuth %s falló: %s", provider, e)
        return _front({"provider": provider, "error": "No se pudo completar la conexión"})
    return _front({"provider": provider, "connected": "1"})


# --- Webhooks públicos ----------------------------------------------------------------------------------------
@router.post("/webhooks/hub/{provider}/{conn_id}")
async def hub_webhook(provider: str, conn_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.body()
    conn = await session.get(IntegrationConnection, conn_id)
    if not conn or conn.provider != provider or conn.status == "disabled":
        raise HTTPException(404, "Conexión no encontrada")
    headers = {k.lower(): v for k, v in request.headers.items()}
    secret = await common.secret(session, conn, "webhook_secret")
    if provider == "shopify":
        secret = secret or ((conn.settings or {}).get("oauth") and get_settings().shopify_client_secret) or None
        if not commerce.verify_shopify(secret or "", body, headers.get("x-shopify-hmac-sha256")):
            raise HTTPException(401, "Firma inválida")
        if not headers.get("x-shopify-topic", "").startswith("orders/"):
            return {"ok": True, "ignored": True}
        order = commerce.normalize_shopify_webhook(json.loads(body or b"{}"))
    elif provider == "woocommerce":
        if not headers.get("x-wc-webhook-signature") and body.startswith(b"webhook_id="):
            return {"ok": True}  # ping de creación del webhook
        if not commerce.verify_woo(secret or "", body, headers.get("x-wc-webhook-signature")):
            raise HTTPException(401, "Firma inválida")
        if not headers.get("x-wc-webhook-topic", "").startswith("order."):
            return {"ok": True, "ignored": True}
        order = commerce.normalize_woo_order(json.loads(body or b"{}"))
    elif provider == "vtex":
        data = json.loads(body or b"{}")
        if data.get("hookConfig") == "ping":
            return {"ok": True}
        if not commerce.verify_vtex(secret or "", headers.get("x-hub-secret")):
            raise HTTPException(401, "Firma inválida")
        if not data.get("OrderId"):
            return {"ok": True, "ignored": True}
        try:
            order = await (await commerce.client_for(session, conn)).order(str(data["OrderId"]))
        except HubError as e:
            raise HTTPException(502 if e.retryable else 400, "No se pudo leer el pedido de VTEX") from None
        if not order:
            return {"ok": True, "ignored": True}
    elif provider == "custom":
        defn = await session.get(ConnectorDefinition, conn.connector_id) if conn.connector_id else None
        if not defn or not builder.verify_webhook(defn, secret, headers, body):
            raise HTTPException(401, "Firma inválida")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "JSON inválido") from None
        return {"ok": True, **await builder.apply_webhook(session, conn, defn, payload)}
    else:
        raise HTTPException(404, "Proveedor no soportado")
    run = await common.start_run(session, conn, "order", "webhook")
    run.fetched = 1
    try:
        async with session.begin_nested():
            res = await common.upsert_order(session, conn, order)
        setattr(run, res, getattr(run, res) + 1)
    except Exception as e:  # noqa: BLE001
        common.note_error(run, order.get("external_id"), e)
    common.finish_run(run)
    await session.commit()
    return {"ok": True}


# --- Conectores propios -----------------------------------------------------------------------------------------
class ConnectorIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    key: str | None = None
    description: str | None = None
    base_url: str
    auth: dict = {}
    endpoints: list[dict] = []
    mappings: dict = {}
    webhooks: dict = {}
    is_published: bool = False


async def _connector(session: AsyncSession, agent: Agent, cid: int, writable: bool = True) -> ConnectorDefinition:
    d = await session.get(ConnectorDefinition, cid)
    if not d or d.organization_id not in (agent.organization_id, None) or (writable and d.organization_id is None):
        raise HTTPException(404, "Conector no encontrado")
    return d


async def ensure_templates(session: AsyncSession) -> None:
    have = set((await session.scalars(select(ConnectorDefinition.key).where(
        ConnectorDefinition.organization_id.is_(None)))).all())
    added = False
    for t in builder.template_definitions():
        if t["key"] not in have:
            session.add(ConnectorDefinition(organization_id=None, is_published=True, **t))
            added = True
    if added:
        await session.commit()


@router.get("/api/hub/connectors")
async def list_connectors(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    await ensure_templates(session)
    rows = (await session.scalars(select(ConnectorDefinition).where(or_(
        ConnectorDefinition.organization_id == agent.organization_id, ConnectorDefinition.organization_id.is_(None)))
        .order_by(ConnectorDefinition.organization_id.is_(None), ConnectorDefinition.name))).all()
    return [builder.definition_json(d) for d in rows]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "conector"


@router.post("/api/hub/connectors")
async def create_connector(body: ConnectorIn, from_template: int | None = None, agent: Agent = Depends(MANAGE),
                           session: AsyncSession = Depends(get_session)):
    data = body.model_dump()
    if from_template:
        tpl = await _connector(session, agent, from_template, writable=False)
        data = {**builder.definition_json(tpl), "name": body.name, "description": body.description or tpl.description,
                "base_url": body.base_url or tpl.base_url}
    errors = builder.validate(data)
    if errors:
        raise HTTPException(422, {"errors": errors})
    key = _slug(body.key or body.name)
    if await session.scalar(select(ConnectorDefinition.id).where(
            ConnectorDefinition.organization_id == agent.organization_id, ConnectorDefinition.key == key)):
        key = f"{key}_{int(utcnow().timestamp())}"
    d = ConnectorDefinition(organization_id=agent.organization_id, key=key, name=data["name"],
                            description=data.get("description"), base_url=data["base_url"].rstrip("/"),
                            auth=data.get("auth") or {}, endpoints=data.get("endpoints") or [],
                            mappings=data.get("mappings") or {}, webhooks=data.get("webhooks") or {},
                            is_published=bool(data.get("is_published")), created_by=agent.id)
    session.add(d)
    await session.commit()
    return builder.definition_json(d)


@router.put("/api/hub/connectors/{cid}")
async def update_connector(cid: int, body: ConnectorIn, agent: Agent = Depends(MANAGE),
                           session: AsyncSession = Depends(get_session)):
    d = await _connector(session, agent, cid)
    data = body.model_dump()
    errors = builder.validate(data)
    if errors:
        raise HTTPException(422, {"errors": errors})
    d.name, d.description, d.base_url = data["name"], data.get("description"), data["base_url"].rstrip("/")
    d.auth, d.endpoints, d.mappings, d.webhooks = data["auth"], data["endpoints"], data["mappings"], data["webhooks"]
    d.is_published = data["is_published"]
    d.version += 1
    d.updated_at = utcnow()
    await session.commit()
    return builder.definition_json(d)


@router.post("/api/hub/connectors/validate")
async def validate_connector(body: ConnectorIn, agent: Agent = Depends(current_agent)):
    return {"errors": builder.validate(body.model_dump())}


@router.delete("/api/hub/connectors/{cid}", status_code=204)
async def delete_connector(cid: int, agent: Agent = Depends(MANAGE), session: AsyncSession = Depends(get_session)):
    d = await _connector(session, agent, cid)
    if await session.scalar(select(IntegrationConnection.id).where(IntegrationConnection.connector_id == d.id).limit(1)):
        raise HTTPException(409, "El conector tiene conexiones: elimínalas primero")
    await session.delete(d)
    await session.commit()


class CustomConnIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    secrets: dict[str, str] = {}
    settings: dict = {}


@router.post("/api/hub/connectors/{cid}/connections")
async def create_custom_connection(cid: int, body: CustomConnIn, agent: Agent = Depends(MANAGE),
                                   session: AsyncSession = Depends(get_session)):
    d = await _connector(session, agent, cid)
    await _label_free(session, agent.organization_id, "custom", body.label)
    bad = [k for k in body.secrets if k not in builder.SECRET_NAMES]
    if bad:
        raise HTTPException(422, f"Secretos desconocidos: {', '.join(bad)}")
    conn = IntegrationConnection(organization_id=agent.organization_id, provider="custom", connector_id=d.id,
                                 label=body.label.strip(), status="connected", instance_url=d.base_url,
                                 connected_by=agent.id, scopes=[],
                                 settings={k: v for k, v in body.settings.items() if k in SETTING_KEYS})
    session.add(conn)
    await session.flush()
    for name, value in body.secrets.items():
        if value:
            await common.set_secret(session, conn, name, value)
    if (conn.settings or {}).get("crm_push"):
        from app.models import IntegrationMapping

        for mp in cx.default_mappings("custom"):
            session.add(IntegrationMapping(connection_id=conn.id, **mp))
    await session.commit()
    return await conn_json(session, conn)


class TestIn(BaseModel):
    endpoint: str
    entity: str | None = None


@router.post("/api/hub/connections/{conn_id}/test")
async def test_endpoint(conn_id: int, body: TestIn, agent: Agent = Depends(MANAGE),
                        session: AsyncSession = Depends(get_session)):
    """«Probar endpoint»: primera página (máx. 5 ítems) + cómo quedaría mapeada. Secretos enmascarados."""
    conn = await _own(session, agent, conn_id, ("custom",))
    try:
        return await builder.test_endpoint(session, conn, body.endpoint, body.entity)
    except HubError as e:
        raise HTTPException(400, str(e)) from None


# --- Exportación de datos -----------------------------------------------------------------------------------------
@router.get("/api/hub/exports/datasets")
async def export_datasets(agent: Agent = Depends(EXPORTS)):
    return {"datasets": exports.catalog(), "parquet": exports.parquet_available(),
            "destinations": list(exports.DESTINATIONS), "schedules": list(exports.SCHEDULES)}


class ExportIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    destination: str
    config: dict = {}
    secrets: dict[str, str] = {}
    datasets: list[str]
    format: str = "parquet"
    schedule: str = "daily"
    incremental: bool = True
    include_sensitive: bool = False
    is_active: bool = True


async def _check_sensitive(session: AsyncSession, agent: Agent, body: ExportIn) -> None:
    if not body.include_sensitive:
        return
    need = ["exports.contacts"] + (["exports.conversations"] if "messages" in body.datasets else [])
    for p in need:
        if not await has_permission(session, agent, p):
            raise HTTPException(403, "Incluir datos sensibles requiere permiso para exportar clientes y conversaciones")


async def _export(session: AsyncSession, agent: Agent, eid: int) -> DataExport:
    e = await session.get(DataExport, eid)
    if not e or e.organization_id != agent.organization_id:
        raise HTTPException(404, "Exportación no encontrada")
    return e


@router.get("/api/hub/exports")
async def list_exports(agent: Agent = Depends(EXPORTS), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(DataExport).where(DataExport.organization_id == agent.organization_id)
                                  .order_by(DataExport.id))).all()
    return [exports.export_json(e) for e in rows]


@router.post("/api/hub/exports")
async def create_export(body: ExportIn, agent: Agent = Depends(EXPORTS), session: AsyncSession = Depends(get_session)):
    cfg = {k: v for k, v in body.config.items() if k != "secret_ids"}
    errors = exports.validate(body.destination, body.datasets, body.format, body.schedule, cfg)
    if errors:
        raise HTTPException(422, {"errors": errors})
    await _check_sensitive(session, agent, body)
    e = DataExport(organization_id=agent.organization_id, name=body.name, destination=body.destination, config=cfg,
                   datasets=body.datasets, format=body.format, schedule=body.schedule, incremental=body.incremental,
                   include_sensitive=body.include_sensitive, is_active=body.is_active, created_by=agent.id)
    session.add(e)
    await session.flush()
    await exports.save_secrets(session, e, body.secrets)
    await session.commit()
    return exports.export_json(e)


@router.put("/api/hub/exports/{eid}")
async def update_export(eid: int, body: ExportIn, agent: Agent = Depends(EXPORTS),
                        session: AsyncSession = Depends(get_session)):
    e = await _export(session, agent, eid)
    cfg = {**{k: v for k, v in body.config.items() if k != "secret_ids"},
           "secret_ids": (e.config or {}).get("secret_ids") or {}}
    errors = exports.validate(body.destination, body.datasets, body.format, body.schedule, cfg)
    if errors:
        raise HTTPException(422, {"errors": errors})
    await _check_sensitive(session, agent, body)
    e.name, e.destination, e.config, e.datasets = body.name, body.destination, cfg, body.datasets
    e.format, e.schedule, e.incremental = body.format, body.schedule, body.incremental
    e.include_sensitive, e.is_active = body.include_sensitive, body.is_active
    await exports.save_secrets(session, e, body.secrets)
    await session.commit()
    return exports.export_json(e)


@router.delete("/api/hub/exports/{eid}", status_code=204)
async def delete_export(eid: int, agent: Agent = Depends(EXPORTS), session: AsyncSession = Depends(get_session)):
    e = await _export(session, agent, eid)
    for sid in ((e.config or {}).get("secret_ids") or {}).values():
        await delete_secret(session, sid)
    await session.execute(delete(DataExportRun).where(DataExportRun.export_id == e.id))
    await session.delete(e)
    await session.commit()


@router.post("/api/hub/exports/{eid}/run")
async def run_export_now(eid: int, full: bool = False, agent: Agent = Depends(EXPORTS),
                         session: AsyncSession = Depends(get_session)):
    e = await _export(session, agent, eid)
    if jobs.enabled():
        await jobs.enqueue(session, "hub.export", {"export_id": e.id, "full": full},
                           organization_id=e.organization_id, dedupe_key=f"export:{e.id}")
        await session.commit()
        return {"queued": True}
    run = await exports.run_export(session, e, full=full)
    return {"queued": False, "run": exports.run_json(run)}


@router.get("/api/hub/exports/{eid}/runs")
async def export_runs(eid: int, limit: int = Query(30, le=200), agent: Agent = Depends(EXPORTS),
                      session: AsyncSession = Depends(get_session)):
    e = await _export(session, agent, eid)
    rows = (await session.scalars(select(DataExportRun).where(DataExportRun.export_id == e.id)
                                  .order_by(DataExportRun.started_at.desc()).limit(limit))).all()
    return [exports.run_json(r) for r in rows]


@router.get("/api/hub/exports/{eid}/runs/{run_id}/files/{index}")
async def download_file(eid: int, run_id: int, index: int, agent: Agent = Depends(EXPORTS),
                        session: AsyncSession = Depends(get_session)):
    e = await _export(session, agent, eid)
    run = await session.get(DataExportRun, run_id)
    if not run or run.export_id != e.id or e.destination != "download" or not (0 <= index < len(run.files or [])):
        raise HTTPException(404, "Archivo no encontrado")
    f = run.files[index]
    if not f.get("target"):
        raise HTTPException(404, "Archivo no encontrado")
    data = await storage.download(f["target"])
    mime = {"csv": "text/csv", "jsonl": "application/x-ndjson", "parquet": "application/vnd.apache.parquet"}.get(
        f.get("format"), "application/octet-stream")
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="{f["target"].rsplit("/", 1)[-1]}"'})
