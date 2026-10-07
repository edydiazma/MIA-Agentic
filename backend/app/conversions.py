"""Conversiones: registro (conversion_events) y envío a Google Ads (offline click conversions)
y Meta CAPI (business messaging). Ver docs/data-model.md §10.1.

- record(): crea eventos para las acciones activas que coinciden con el disparador (deduplicado por dedupe_key)
  y encola un envío por destino configurado (o 'skipped' si faltan identificadores de clic).
- conversions_loop(): cada 60 s detecta conversiones nuevas (cierres con tipificación, negocios ganados,
  citas agendadas, contactos que pasan a etapa 'client') con un cursor por organización en org_settings
  ('conversions_cursor'), y sube los envíos pendientes con reintentos exponenciales.
"""

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Alert,
    Appointment,
    Attribution,
    Channel,
    Contact,
    ContactChange,
    ConversionAction,
    ConversionEvent,
    ConversionUpload,
    Conversation,
    ConversationEvent,
    Deal,
    IntegrationConnection,
    OrgSetting,
    utcnow,
)
from app.secrets_vault import get_secret, put_secret

log = logging.getLogger(__name__)
GOOGLE_ADS_API = "https://googleads.googleapis.com/v20"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Misma base y versión que WhatsApp (WA_GRAPH_BASE permite apuntar a un mock en pruebas de carga)
META_GRAPH = f"{get_settings().wa_graph_base.rstrip('/')}/{get_settings().wa_api_version}"
BACKOFF_S = [60, 300, 1800, 7200, 43200]  # 1 min, 5 min, 30 min, 2 h, 12 h
MAX_ATTEMPTS = 6
CURSOR_KEY = "conversions_cursor"
SNAPSHOT_FIELDS = ("channel", "utm_source", "utm_medium", "utm_campaign", "utm_term", "gclid", "gbraid", "wbraid",
                   "fbc", "fbp", "ctwa_clid", "ad_id", "landing_url", "link_id", "platform_campaign_name")


def http_client(timeout: float = 30) -> httpx.AsyncClient:
    """Cliente HTTP de los envíos (punto único para simularlo en pruebas)."""
    return httpx.AsyncClient(timeout=timeout)


class UploadError(Exception):
    """Fallo de envío. retry=False marca el envío como fallido sin más intentos."""

    def __init__(self, message: str, retry: bool = True, response: dict | None = None):
        super().__init__(message)
        self.retry = retry
        self.response = response


# --- Normalización y hash (SHA-256 de datos normalizados) ------------------------
def phone_digits(wa_id: str | None) -> str | None:
    d = re.sub(r"\D", "", wa_id or "")
    return d or None


def sha256(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def hash_phone(wa_id: str | None) -> str | None:
    """Meta: dígitos con código de país, sin '+'."""
    return sha256(phone_digits(wa_id))


def hash_phone_e164(wa_id: str | None) -> str | None:
    """Google Ads (enhanced conversions): E.164 con '+'."""
    d = phone_digits(wa_id)
    return sha256(f"+{d}") if d else None


def hash_email(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return sha256(e) if e else None


# --- Registro ---------------------------------------------------------------------
async def _snapshot(session: AsyncSession, conversation_id: int | None, contact_id: int) -> dict:
    stmt = select(Attribution)
    stmt = stmt.where(Attribution.conversation_id == conversation_id) if conversation_id else \
        stmt.where(Attribution.contact_id == contact_id).order_by(Attribution.created_at.desc())
    a = (await session.scalars(stmt.limit(1))).first()
    if not a and conversation_id:  # sin atribución en esta conversación: la más reciente del contacto
        a = (await session.scalars(select(Attribution).where(Attribution.contact_id == contact_id)
                                   .order_by(Attribution.created_at.desc()).limit(1))).first()
    return {f: getattr(a, f) for f in SNAPSHOT_FIELDS if a is not None and getattr(a, f) is not None}


def _destinations(action: ConversionAction, attr: dict) -> list[tuple[str, str, str | None]]:
    """[(destino, estado, motivo)] para cada destino configurado en la acción."""
    out = []
    g = action.google_ads or {}
    if g.get("customer_id") and g.get("conversion_action_id"):
        if attr.get("gclid") or attr.get("gbraid") or attr.get("wbraid"):
            out.append(("google_ads", "pending", None))
        else:
            out.append(("google_ads", "skipped", "La conversación no tiene gclid/gbraid/wbraid"))
    m = action.meta or {}
    if m.get("dataset_id"):
        if attr.get("ctwa_clid") or attr.get("fbc"):
            out.append(("meta_capi", "pending", None))
        else:
            out.append(("meta_capi", "skipped", "La conversación no tiene ctwa_clid ni fbc"))
    return out


async def record(session: AsyncSession, org: int, trigger: str, conversation: Conversation | None = None,
                 contact: Contact | None = None, typification_id: int | None = None, deal: Deal | None = None,
                 occurred_at: datetime | None = None) -> list[ConversionEvent]:
    """Crea las conversiones que correspondan (idempotente). No hace commit."""
    stmt = select(ConversionAction).where(ConversionAction.organization_id == org, ConversionAction.is_active,
                                          ConversionAction.trigger == trigger)
    if trigger == "typification":
        stmt = stmt.where(ConversionAction.typification_id == typification_id)
    actions = (await session.scalars(stmt)).all()
    if not actions:
        return []
    if contact is None:
        contact_id = conversation.contact_id if conversation else deal.contact_id
        contact = await session.get(Contact, contact_id)
    conversation_id = conversation.id if conversation else (deal.conversation_id if deal else None)
    attr = await _snapshot(session, conversation_id, contact.id)

    created = []
    for action in actions:
        key = f"{action.id}:{conversation_id or contact.id}:{deal.id if deal else ''}"
        if await session.scalar(select(ConversionEvent.id).where(
                ConversionEvent.organization_id == org, ConversionEvent.dedupe_key == key)):
            continue
        value = action.value if action.value is not None else (deal.amount if deal else None)
        ev = ConversionEvent(organization_id=org, action_id=action.id, conversation_id=conversation_id,
                             contact_id=contact.id, value=value,
                             currency=(deal.currency if deal and action.value is None else action.currency),
                             occurred_at=occurred_at or utcnow(), attribution=attr,
                             hashed_phone=hash_phone(contact.wa_id), hashed_email=hash_email(contact.email),
                             dedupe_key=key)
        session.add(ev)
        await session.flush()
        for dest, status, reason in _destinations(action, attr):
            session.add(ConversionUpload(event_id=ev.id, destination=dest, status=status, error=reason))
        created.append(ev)
    return created


# --- Detección de conversiones (cursor por organización) -------------------------
async def _cursor(session: AsyncSession, org: int) -> tuple[OrgSetting, dict]:
    row = await session.get(OrgSetting, (org, CURSOR_KEY))
    if not row:
        # Primera vez: empieza ahora (no se suben conversiones históricas)
        now = utcnow().isoformat()
        row = OrgSetting(organization_id=org, key=CURSOR_KEY,
                         value={"events_at": now, "deals_at": now, "appointments_id": 0, "changes_id": 0})
        session.add(row)
        await session.flush()
        last_appt = await session.scalar(select(Appointment.id).where(Appointment.organization_id == org)
                                         .order_by(Appointment.id.desc()).limit(1))
        last_change = await session.scalar(select(ContactChange.id).where(ContactChange.organization_id == org)
                                           .order_by(ContactChange.id.desc()).limit(1))
        row.value = {**row.value, "appointments_id": last_appt or 0, "changes_id": last_change or 0}
    return row, dict(row.value)


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def scan_org(session: AsyncSession, org: int) -> int:
    """Detecta conversiones nuevas de una organización y avanza su cursor. Devuelve cuántas creó."""
    row, cur = await _cursor(session, org)
    triggers = set((await session.scalars(select(ConversionAction.trigger).where(
        ConversionAction.organization_id == org, ConversionAction.is_active))).all())
    created = 0

    # Cierres con tipificación (hechos de la base)
    events = (await session.scalars(
        select(ConversationEvent).where(ConversationEvent.organization_id == org,
                                        ConversationEvent.event_type == "closed",
                                        ConversationEvent.occurred_at > _ts(cur["events_at"]))
        .order_by(ConversationEvent.occurred_at).limit(500))).all()
    for e in events:
        typ = (e.payload or {}).get("typification_id")
        if typ and "typification" in triggers:
            conv = await session.get(Conversation, e.conversation_id)
            if conv:
                created += len(await record(session, org, "typification", conversation=conv, typification_id=typ,
                                            occurred_at=e.occurred_at))
        cur["events_at"] = e.occurred_at.isoformat()

    # Negocios ganados
    deals = (await session.scalars(
        select(Deal).where(Deal.organization_id == org, Deal.status == "won", Deal.closed_at > _ts(cur["deals_at"]))
        .order_by(Deal.closed_at).limit(500))).all()
    for d in deals:
        if "deal_won" in triggers:
            conv = await session.get(Conversation, d.conversation_id) if d.conversation_id else None
            created += len(await record(session, org, "deal_won", conversation=conv, deal=d, occurred_at=d.closed_at))
        cur["deals_at"] = d.closed_at.isoformat()

    # Citas agendadas
    appts = (await session.scalars(
        select(Appointment).where(Appointment.organization_id == org, Appointment.id > cur["appointments_id"])
        .order_by(Appointment.id).limit(500))).all()
    for a in appts:
        if "appointment_booked" in triggers:
            conv = await session.get(Conversation, a.conversation_id) if a.conversation_id else None
            contact = await session.get(Contact, a.contact_id)
            created += len(await record(session, org, "appointment_booked", conversation=conv, contact=contact,
                                        occurred_at=a.created_at))
        cur["appointments_id"] = a.id

    # Contactos que pasan a etapa "client"
    changes = (await session.scalars(
        select(ContactChange).where(ContactChange.organization_id == org, ContactChange.id > cur["changes_id"])
        .order_by(ContactChange.id).limit(1000))).unique().all()
    for ch in changes:
        if ch.field_key == "stage" and ch.new_value == "client" and "stage_client" in triggers:
            contact = await session.get(Contact, ch.contact_id)
            conv = await session.get(Conversation, ch.conversation_id) if ch.conversation_id else None
            created += len(await record(session, org, "stage_client", conversation=conv, contact=contact,
                                        occurred_at=ch.created_at))
        cur["changes_id"] = ch.id

    row.value = cur  # reasignar para detectar el cambio del JSON
    await session.commit()
    return created


async def scan_all() -> int:
    async with SessionLocal() as session:
        orgs = sorted(set((await session.scalars(
            select(ConversionAction.organization_id).where(ConversionAction.is_active))).all()))
    total = 0
    for org in orgs:
        async with SessionLocal() as session:
            total += await scan_org(session, org)
    return total


# --- Envíos -------------------------------------------------------------------------
async def _connection(session: AsyncSession, org: int, provider: str) -> IntegrationConnection | None:
    return (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider == provider))).first()


async def google_access_token(session: AsyncSession, conn: IntegrationConnection) -> str:
    """Token de Google Ads vigente; lo renueva con el refresh token si está por vencer."""
    env = get_settings()
    token = await get_secret(session, conn.access_token_secret_id)
    if token and conn.expires_at and conn.expires_at > utcnow() + timedelta(seconds=60):
        return token
    refresh = await get_secret(session, conn.refresh_token_secret_id)
    if not refresh:
        raise UploadError("Google Ads no tiene refresh token: vuelve a conectar la cuenta", retry=False)
    async with http_client(20) as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={
            "client_id": env.google_oauth_client_id, "client_secret": env.google_oauth_client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code >= 400:
        conn.status, conn.last_error = "error", r.text[:1000]
        raise UploadError(f"No se pudo renovar el token de Google: {r.text[:300]}", retry=r.status_code >= 500)
    data = r.json()
    conn.access_token_secret_id = await put_secret(session, data["access_token"], f"google_ads_access:{conn.id}",
                                                   conn.access_token_secret_id)
    conn.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
    return data["access_token"]


def _google_datetime(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")


async def upload_google(session: AsyncSession, up: ConversionUpload) -> dict:
    env = get_settings()
    ev = up.event
    cfg = ev.action.google_ads or {}
    conn = await _connection(session, ev.organization_id, "google_ads")
    if not conn or conn.status == "revoked":
        raise UploadError("Google Ads no está conectado", retry=False)
    if not env.google_ads_developer_token:
        raise UploadError("Falta GOOGLE_ADS_DEVELOPER_TOKEN en el servidor", retry=False)
    token = await google_access_token(session, conn)
    cid = str(cfg["customer_id"]).replace("-", "")
    attr = ev.attribution or {}
    contact = await session.get(Contact, ev.contact_id)
    conversion: dict = {
        "conversionAction": f"customers/{cid}/conversionActions/{cfg['conversion_action_id']}",
        "conversionDateTime": _google_datetime(ev.occurred_at),
        "orderId": ev.dedupe_key,
    }
    for key in ("gclid", "gbraid", "wbraid"):
        if attr.get(key):
            conversion[key] = attr[key]
            break
    if ev.value is not None:
        conversion["conversionValue"] = float(ev.value)
        conversion["currencyCode"] = ev.currency
    identifiers = []
    if hp := hash_phone_e164(contact.wa_id if contact else None):
        identifiers.append({"hashedPhoneNumber": hp})
    if ev.hashed_email:
        identifiers.append({"hashedEmail": ev.hashed_email})
    if identifiers:
        conversion["userIdentifiers"] = identifiers
    headers = {"Authorization": f"Bearer {token}", "developer-token": env.google_ads_developer_token}
    login = (conn.settings or {}).get("login_customer_id") or env.google_ads_login_customer_id
    if login:
        headers["login-customer-id"] = str(login).replace("-", "")
    async with http_client(30) as http:
        r = await http.post(f"{GOOGLE_ADS_API}/customers/{cid}:uploadClickConversions", headers=headers,
                            json={"conversions": [conversion], "partialFailure": True})
    body = _json(r)
    if r.status_code >= 400:
        raise UploadError(f"Google Ads {r.status_code}: {str(body)[:500]}", retry=r.status_code >= 500 or r.status_code == 429,
                          response=body)
    if body.get("partialFailureError"):
        raise UploadError(f"Google Ads rechazó la conversión: {body['partialFailureError'].get('message', '')[:500]}",
                          retry=False, response=body)
    return body


async def upload_meta(session: AsyncSession, up: ConversionUpload) -> dict:
    env = get_settings()
    ev = up.event
    cfg = ev.action.meta or {}
    conn = await _connection(session, ev.organization_id, "meta")
    token = env.meta_capi_token or (await get_secret(session, conn.access_token_secret_id) if conn else None)
    if not token:
        raise UploadError("Falta el token de Meta (META_CAPI_TOKEN o conexión Meta)", retry=False)
    attr = ev.attribution or {}
    user_data: dict = {}
    if attr.get("ctwa_clid"):
        user_data["ctwa_clid"] = attr["ctwa_clid"]
    if attr.get("fbc"):
        user_data["fbc"] = attr["fbc"]
    if attr.get("fbp"):
        user_data["fbp"] = attr["fbp"]
    if ev.hashed_phone:
        user_data["ph"] = [ev.hashed_phone]
    if ev.hashed_email:
        user_data["em"] = [ev.hashed_email]
    waba = cfg.get("whatsapp_business_account_id") or (conn.external_account_id if conn else None)
    if not waba and ev.conversation_id:
        conv = await session.get(Conversation, ev.conversation_id)
        channel = await session.get(Channel, conv.channel_id) if conv else None
        waba = channel.waba_id if channel else None
    if waba:
        user_data["whatsapp_business_account_id"] = waba
    event: dict = {
        "event_name": cfg.get("event_name") or "Purchase",
        "event_time": int(ev.occurred_at.timestamp()),
        "event_id": ev.dedupe_key,
        "user_data": user_data,
    }
    if attr.get("ctwa_clid"):  # conversión de Click to WhatsApp
        event.update({"action_source": "business_messaging", "messaging_channel": "whatsapp"})
    else:  # vino de un anuncio a la web (fbc)
        event["action_source"] = "website"
        if attr.get("landing_url"):  # Meta lo exige en eventos de sitio web
            event["event_source_url"] = attr["landing_url"]
    if ev.value is not None:
        event["custom_data"] = {"value": float(ev.value), "currency": ev.currency}
    async with http_client(30) as http:
        r = await http.post(f"{META_GRAPH}/{cfg['dataset_id']}/events",
                            json={"data": [event], "access_token": token})
    body = _json(r)
    if r.status_code >= 400:
        raise UploadError(f"Meta {r.status_code}: {str(body)[:500]}", retry=r.status_code >= 500 or r.status_code == 429,
                          response=body)
    return body


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"text": r.text[:2000]}


UPLOADERS = {"google_ads": upload_google, "meta_capi": upload_meta}


async def process_upload(session: AsyncSession, up: ConversionUpload) -> None:
    """Intenta un envío y aplica backoff exponencial; tras el último intento queda 'failed' con una alerta."""
    up.attempts += 1
    try:
        up.response = await UPLOADERS[up.destination](session, up)
        up.status, up.sent_at, up.error = "sent", utcnow(), None
    except UploadError as e:
        up.error, up.response = str(e)[:2000], e.response
        if e.retry and up.attempts < MAX_ATTEMPTS:
            up.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF_S[min(up.attempts - 1, len(BACKOFF_S) - 1)])
        else:
            up.status = "failed"
            session.add(Alert(organization_id=up.event.organization_id, severity="warning", layer="conversion",
                              source="system", title=f"No se pudo enviar una conversión a "
                              f"{'Google Ads' if up.destination == 'google_ads' else 'Meta'}",
                              description=up.error, ref=str(up.event_id)))
    except Exception as e:  # red, timeouts: reintentable
        up.error = f"{type(e).__name__}: {e}"[:2000]
        if up.attempts < MAX_ATTEMPTS:
            up.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF_S[min(up.attempts - 1, len(BACKOFF_S) - 1)])
        else:
            up.status = "failed"
    await session.commit()


async def upload_due(limit: int = 100) -> int:
    async with SessionLocal() as session:
        ups = (await session.scalars(select(ConversionUpload).where(
            ConversionUpload.status == "pending", ConversionUpload.next_attempt_at <= utcnow())
            .order_by(ConversionUpload.next_attempt_at).limit(limit))).unique().all()
        for up in ups:
            await process_upload(session, up)
    return len(ups)


async def schedule_due(limit: int = 500) -> int:
    from app import jobs

    async with SessionLocal() as session:
        rows = (await session.execute(select(ConversionUpload.id, ConversionEvent.organization_id)
                                      .join(ConversionEvent, ConversionEvent.id == ConversionUpload.event_id)
                                      .where(ConversionUpload.status == "pending",
                                             ConversionUpload.next_attempt_at <= utcnow())
                                      .order_by(ConversionUpload.next_attempt_at).limit(limit))).all()
    return await jobs.schedule("conversions.upload", [({"upload_id": i}, f"cu:{i}", o) for i, o in rows])


async def conversions_loop() -> None:
    """Registrar en main.py (lifespan): detecta conversiones y sube envíos cada 60 s."""
    while True:
        await asyncio.sleep(60)
        try:
            created = await scan_all()
            from app import jobs

            if jobs.enabled():  # cada envío es un trabajo (cola "conversions"); el backoff lo lleva el envío
                sent = await schedule_due()
            else:
                sent = await upload_due()
            if created or sent:
                log.info("Conversiones: %s nuevas, %s envíos procesados", created, sent)
        except Exception:
            log.exception("Falló el ciclo de conversiones")
