"""Motor de sincronización con el CRM.

Push (local → CRM): una cola (integration_outbox) con reintentos y backoff. Un escáner encola contactos y
negocios cambiados y notas de conversaciones cerradas. Cada envío guarda en external_links el hash de lo
enviado: si nada cambió, no se reenvía.

Pull (CRM → local): lectura incremental de cambios remotos. Si el registro remoto coincide con el último hash
(eco de nuestro propio envío) se ignora; si no, se aplica con fuente 'crm' y se recalcula el hash, así el
cambio no vuelve al CRM (sin bucles).
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crm import attribution_fields
from app.crm.base import CRM_PROVIDERS, CRMAdapter, CRMError, RemoteRecord, TokenRevoked, payload_hash
from app.crm.connections import adapter_for
from app.db import SessionLocal
from app.fields import coerce, fields_by_key, set_custom, set_native
from app.models import (
    Alert,
    Contact,
    Conversation,
    ConversationEvent,
    Deal,
    ExternalLink,
    IntegrationConnection,
    IntegrationMapping,
    IntegrationOutbox,
    utcnow,
)

log = logging.getLogger(__name__)
BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2),
           timedelta(hours=2)]
MAX_ATTEMPTS = 6
SCAN_BATCH = 500
PULL_EVERY = timedelta(minutes=5)
STAGES = ("lead", "prospect", "client", "lost")
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# --- Valores locales y transformaciones ----------------------------------------
def split_name(name: str | None) -> tuple[str, str]:
    first, _, last = (name or "").strip().partition(" ")
    return first, last.strip()


def contact_value(contact: Contact, field: str, customs: dict, attribution: dict | None = None):
    if attribution_fields.is_attribution(field):
        return (attribution or {}).get(field)
    first, last = split_name(contact.name)
    if field == "name":
        return contact.name
    if field == "first_name":
        return first or None
    if field == "last_name":
        return last or None
    if field == "phone":
        return f"+{contact.wa_id}" if contact.wa_id else None
    if field in ("email", "stage", "notes", "memory"):
        return getattr(contact, field)
    if field.startswith("custom:"):
        return customs.get(field.split(":", 1)[1])
    return None


def deal_value(deal: Deal, field: str, attribution: dict | None = None):
    if attribution_fields.is_attribution(field):
        return (attribution or {}).get(field)
    if field == "deal.name":
        return deal.name
    if field == "deal.amount":
        return float(deal.amount) if deal.amount is not None else None
    if field == "deal.stage":
        return deal.stage
    if field == "deal.status":
        return deal.status
    if field == "deal.currency":
        return deal.currency
    if field == "deal.close_date":
        d = deal.closed_at.date() if deal.closed_at else deal.expected_close
        return d.isoformat() if d else None
    return None


def forward(value, transform: dict | None):
    if value is not None and transform and isinstance(transform.get("map"), dict):
        return transform["map"].get(str(value), value)
    return value


def backward(value, transform: dict | None):
    if value is not None and transform and isinstance(transform.get("map"), dict):
        inverse = {str(v): k for k, v in transform["map"].items()}
        return inverse.get(str(value), value)
    return value


async def mappings_for(session: AsyncSession, conn_id: int, obj: str) -> list[IntegrationMapping]:
    return list((await session.scalars(select(IntegrationMapping).where(
        IntegrationMapping.connection_id == conn_id, IntegrationMapping.object == obj))).all())


def push_props(mappings: list[IntegrationMapping], value_fn) -> dict:
    props = {}
    for m in mappings:
        if m.direction in ("push", "both"):
            v = forward(value_fn(m.local_field), m.transform)
            if v is not None and v != "":
                props[m.remote_property] = v
    return props


async def contact_push_props(session: AsyncSession, conn: IntegrationConnection, contact: Contact) -> dict:
    from app.fields import custom_values

    await session.refresh(contact, ["field_values"])
    customs = custom_values(contact)
    mappings = await mappings_for(session, conn.id, "contact")
    attribution = (await attribution_fields.load(session, contact.id)
                   if any(attribution_fields.is_attribution(m.local_field) for m in mappings) else {})
    return push_props(mappings, lambda f: contact_value(contact, f, customs, attribution))


async def deal_push_props(session: AsyncSession, conn: IntegrationConnection, deal: Deal) -> dict:
    mappings = await mappings_for(session, conn.id, "deal")
    attribution = (await attribution_fields.load(session, deal.contact_id, deal.conversation_id)
                   if any(attribution_fields.is_attribution(m.local_field) for m in mappings) else {})
    return push_props(mappings, lambda f: deal_value(deal, f, attribution))


async def get_link(session: AsyncSession, conn_id: int, local_type: str, local_id: int) -> ExternalLink | None:
    return await session.scalar(select(ExternalLink).where(
        ExternalLink.connection_id == conn_id, ExternalLink.local_type == local_type, ExternalLink.local_id == local_id))


async def save_link(session: AsyncSession, conn_id: int, local_type: str, local_id: int, remote_type: str,
                    remote_id: str, sync_hash: str | None, remote_updated_at: datetime | None = None) -> ExternalLink:
    link = await get_link(session, conn_id, local_type, local_id)
    # El CRM puede devolver el MISMO registro para dos clientes locales (p. ej. el mismo correo en dos fichas): no se
    # vincula dos veces — se reporta como posible duplicado en vez de romper la cola con un error de integridad.
    taken = await session.scalar(select(ExternalLink.local_id).where(
        ExternalLink.connection_id == conn_id, ExternalLink.remote_type == remote_type,
        ExternalLink.remote_id == remote_id, ExternalLink.local_id != local_id))
    if taken is not None:
        raise CRMError(f"El registro {remote_type} {remote_id} del CRM ya está vinculado al {local_type} #{taken}: "
                       f"posible duplicado (mismo correo o teléfono). Únelos en Clientes → Posibles duplicados.",
                       retryable=False)
    if not link:
        link = ExternalLink(connection_id=conn_id, local_type=local_type, local_id=local_id, remote_type=remote_type,
                            remote_id=remote_id)
        session.add(link)
    link.remote_id, link.sync_hash, link.last_synced_at = remote_id, sync_hash, utcnow()
    if remote_updated_at:
        link.remote_updated_at = remote_updated_at
    return link


def contact_remote_type(conn: IntegrationConnection) -> str:
    if conn.provider in ("hubspot", "custom"):
        return "contact"
    return {"zoho": "Contacts", "odoo": "res.partner"}.get(conn.provider) or (conn.settings or {}).get(
        "contact_object", "Contact")


def deal_remote_type(conn: IntegrationConnection) -> str:
    return {"hubspot": "deal", "custom": "deal", "zoho": "Deals", "odoo": "crm.lead"}.get(conn.provider, "Opportunity")


# --- Push -----------------------------------------------------------------------
async def push_contact(session: AsyncSession, conn: IntegrationConnection, adapter: CRMAdapter,
                       contact: Contact) -> tuple[str, bool]:
    """Devuelve (id remoto, enviado). No reenvía si el hash no cambió."""
    props = await contact_push_props(session, conn, contact)
    h = payload_hash(props)
    link = await get_link(session, conn.id, "contact", contact.id)
    if link and link.sync_hash == h:
        return link.remote_id, False
    remote_id = link.remote_id if link else await adapter.find_contact(contact.email, f"+{contact.wa_id}")
    remote_id = await adapter.upsert_contact(remote_id, props)
    await save_link(session, conn.id, "contact", contact.id, contact_remote_type(conn), remote_id, h)
    return remote_id, True


async def push_deal(session: AsyncSession, conn: IntegrationConnection, adapter: CRMAdapter,
                    deal: Deal) -> tuple[str, bool]:
    contact = await session.get(Contact, deal.contact_id)
    contact_remote, _ = await push_contact(session, conn, adapter, contact)
    props = await deal_push_props(session, conn, deal)
    h = payload_hash(props)
    link = await get_link(session, conn.id, "deal", deal.id)
    if link and link.sync_hash == h:
        return link.remote_id, False
    remote_id = await adapter.upsert_deal(link.remote_id if link else None, props, contact_remote)
    await save_link(session, conn.id, "deal", deal.id, deal_remote_type(conn), remote_id, h)
    return remote_id, True


def note_text(conv: Conversation) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    lines = ["Conversación de WhatsApp cerrada"]
    if conv.typification:
        lines.append(f"Tipificación: {conv.typification.name}")
    if conv.assigned_agent:
        lines.append(f"Asesor: {conv.assigned_agent.name}")
    if conv.ai_summary:
        lines.append(f"Resumen: {conv.ai_summary}")
    if conv.ad_headline:
        lines.append(f"Origen: anuncio «{conv.ad_headline}»")
    lines.append(f"Ver conversación: {base}/conversaciones?id={conv.id}")
    return "\n".join(lines)


async def push_note(session: AsyncSession, conn: IntegrationConnection, adapter: CRMAdapter,
                    conv: Conversation) -> tuple[str, bool]:
    link = await get_link(session, conn.id, "conversation", conv.id)
    note_hash = payload_hash({"closed_at": conv.closed_at})
    if link and link.sync_hash == note_hash:  # una nota por cierre
        return link.remote_id, False
    contact_remote, _ = await push_contact(session, conn, adapter, conv.contact)
    deal = await session.scalar(select(Deal).where(Deal.conversation_id == conv.id).limit(1))
    deal_link = await get_link(session, conn.id, "deal", deal.id) if deal else None
    note_id = await adapter.add_note(contact_remote, note_text(conv), deal_link.remote_id if deal_link else None)
    await save_link(session, conn.id, "conversation", conv.id, {"salesforce": "Task"}.get(conn.provider, "note"),
                    note_id, note_hash)
    return note_id, True


async def _process_row(session: AsyncSession, conn: IntegrationConnection, adapter: CRMAdapter,
                       row: IntegrationOutbox) -> bool:
    if row.entity_type == "contact":
        contact = await session.get(Contact, row.entity_id)
        if not contact or contact.organization_id != conn.organization_id:
            return False
        return (await push_contact(session, conn, adapter, contact))[1]
    if row.entity_type == "deal":
        deal = await session.get(Deal, row.entity_id)
        if not deal or deal.organization_id != conn.organization_id:
            return False
        return (await push_deal(session, conn, adapter, deal))[1]
    conv = await session.get(Conversation, row.entity_id)
    if not conv or conv.organization_id != conn.organization_id:
        return False
    return (await push_note(session, conn, adapter, conv))[1]


async def _alert(session: AsyncSession, conn: IntegrationConnection, title: str, description: str) -> None:
    ref = f"integration:{conn.id}"
    exists = await session.scalar(select(Alert.id).where(
        Alert.organization_id == conn.organization_id, Alert.ref == ref, Alert.resolved_at.is_(None)))
    if not exists:
        session.add(Alert(organization_id=conn.organization_id, severity="critical", layer="integration",
                          source="system", title=title, description=description[:1000], ref=ref))


async def process_outbox(session: AsyncSession, conn: IntegrationConnection, limit: int = 200) -> dict:
    stats = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    rows = (await session.scalars(select(IntegrationOutbox).where(
        IntegrationOutbox.connection_id == conn.id, IntegrationOutbox.status == "pending",
        IntegrationOutbox.next_attempt_at <= utcnow()).order_by(IntegrationOutbox.id).limit(limit))).all()
    if not rows:
        return stats
    adapter = await adapter_for(session, conn)
    refreshed = False
    for row in rows:
        row.attempts += 1
        try:
            try:
                sent = await _process_row(session, conn, adapter, row)
            except TokenRevoked:
                if refreshed or not conn.refresh_token_secret_id:
                    raise
                adapter, refreshed = await adapter_for(session, conn, force_refresh=True), True
                sent = await _process_row(session, conn, adapter, row)
            row.status, row.error, row.sent_at = ("sent" if sent else "skipped"), None, utcnow()
            stats["sent" if sent else "skipped"] += 1
        except TokenRevoked as e:
            row.status, row.error = "pending", str(e)
            conn.status, conn.last_error = "error", str(e)
            await _alert(session, conn, f"{conn.provider.title()}: se perdió la conexión",
                         "El CRM rechazó las credenciales. Vuelve a conectar la cuenta en Configuraciones → Integraciones.")
            await session.commit()
            break
        except CRMError as e:
            row.error = str(e)[:2000]
            if e.retryable and row.attempts < MAX_ATTEMPTS:
                row.next_attempt_at = utcnow() + BACKOFF[min(row.attempts - 1, len(BACKOFF) - 1)]
                stats["retry"] += 1
            else:
                row.status = "failed"
                stats["failed"] += 1
                conn.last_error = row.error
                await _alert(session, conn, f"{conn.provider.title()}: no se pudo sincronizar",
                             f"{row.entity_type} #{row.entity_id}: {row.error}")
        except Exception as e:  # noqa: BLE001 — un registro roto no detiene la cola
            log.exception("Error sincronizando %s #%s", row.entity_type, row.entity_id)
            row.error = f"{type(e).__name__}: {e}"[:2000]
            row.next_attempt_at = utcnow() + BACKOFF[min(row.attempts - 1, len(BACKOFF) - 1)]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
            stats["retry"] += 1
        await session.commit()
    return stats


# --- Encolado y escáner ---------------------------------------------------------
async def _enqueue(session: AsyncSession, conn: IntegrationConnection, entity_type: str, entity_id: int,
                   operation: str = "upsert") -> bool:
    exists = await session.scalar(select(IntegrationOutbox.id).where(
        IntegrationOutbox.connection_id == conn.id, IntegrationOutbox.entity_type == entity_type,
        IntegrationOutbox.entity_id == entity_id, IntegrationOutbox.status == "pending"))
    if exists:
        return False
    session.add(IntegrationOutbox(organization_id=conn.organization_id, connection_id=conn.id,
                                  entity_type=entity_type, entity_id=entity_id, operation=operation))
    return True


async def _connections(session: AsyncSession, org: int) -> list[IntegrationConnection]:
    return [c for c in (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.status == "connected",
        IntegrationConnection.sync_enabled, IntegrationConnection.provider.in_(CRM_PROVIDERS)))).all()
                if c.provider != "custom" or (c.settings or {}).get("crm_push")]


async def enqueue_contact(session: AsyncSession, contact_id: int) -> None:
    contact = await session.get(Contact, contact_id)
    if contact:
        for conn in await _connections(session, contact.organization_id):
            await _enqueue(session, conn, "contact", contact_id)
        await session.commit()


async def enqueue_attribution(session: AsyncSession, contact_id: int) -> None:
    """La atribución del contacto cambió (toque nuevo o nombres de campaña): reenvía el contacto a cada CRM
    conectado que tenga mapeado al menos un campo attribution.*. No hace nada si ninguno lo tiene."""
    contact = await session.get(Contact, contact_id)
    if not contact:
        return
    queued = False
    for conn in await _connections(session, contact.organization_id):
        mapped = await session.scalar(select(IntegrationMapping.id).where(
            IntegrationMapping.connection_id == conn.id, IntegrationMapping.object == "contact",
            IntegrationMapping.local_field.startswith(attribution_fields.PREFIX)).limit(1))
        if mapped:
            queued = await _enqueue(session, conn, "contact", contact_id) or queued
    if queued:
        await session.commit()


async def enqueue_deal(session: AsyncSession, deal_id: int) -> None:
    deal = await session.get(Deal, deal_id)
    if deal:
        for conn in await _connections(session, deal.organization_id):
            await _enqueue(session, conn, "deal", deal_id)
        await session.commit()


async def enqueue_note(session: AsyncSession, conversation_id: int) -> None:
    conv = await session.get(Conversation, conversation_id)
    if conv:
        for conn in await _connections(session, conv.organization_id):
            if (conn.settings or {}).get("push_notes", True):
                await _enqueue(session, conn, "conversation_note", conversation_id, "note")
        await session.commit()


def _parse_cursor(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def scan_changes(session: AsyncSession, conn: IntegrationConnection) -> int:
    """Encola contactos y negocios modificados y notas de conversaciones cerradas desde el último cursor."""
    settings = dict(conn.settings or {})
    cursor = dict(settings.get("cursor") or {})
    queued = 0
    policy = settings.get("push_contacts", "all")
    if policy != "none":
        since = _parse_cursor(cursor.get("contacts"))
        stmt = select(Contact.id, Contact.updated_at).where(
            Contact.organization_id == conn.organization_id, Contact.blocked.is_(False))
        if since:
            stmt = stmt.where(Contact.updated_at > since)
        if policy == "with_deal":
            stmt = stmt.where(Contact.id.in_(select(Deal.contact_id).where(Deal.organization_id == conn.organization_id)))
        rows = (await session.execute(stmt.order_by(Contact.updated_at).limit(SCAN_BATCH))).all()
        for cid, _updated in rows:
            queued += await _enqueue(session, conn, "contact", cid)
        if rows:
            cursor["contacts"] = rows[-1][1].isoformat()
    since = _parse_cursor(cursor.get("deals"))
    stmt = select(Deal.id, Deal.updated_at).where(Deal.organization_id == conn.organization_id)
    if since:
        stmt = stmt.where(Deal.updated_at > since)
    rows = (await session.execute(stmt.order_by(Deal.updated_at).limit(SCAN_BATCH))).all()
    for did, _updated in rows:
        queued += await _enqueue(session, conn, "deal", did)
    if rows:
        cursor["deals"] = rows[-1][1].isoformat()
    if settings.get("push_notes", True):
        since = _parse_cursor(cursor.get("notes")) or (conn.created_at or utcnow())
        rows = (await session.execute(select(ConversationEvent.conversation_id, ConversationEvent.occurred_at).where(
            ConversationEvent.organization_id == conn.organization_id, ConversationEvent.event_type == "closed",
            ConversationEvent.occurred_at > since).order_by(ConversationEvent.occurred_at).limit(SCAN_BATCH))).all()
        for conv_id, _occurred in rows:
            queued += await _enqueue(session, conn, "conversation_note", conv_id, "note")
        if rows:
            cursor["notes"] = rows[-1][1].isoformat()
    settings["cursor"] = cursor
    conn.settings = settings
    await session.commit()
    return queued


# --- Pull -----------------------------------------------------------------------
async def apply_remote_contact(session: AsyncSession, conn: IntegrationConnection, record: RemoteRecord,
                               mappings: list[IntegrationMapping]) -> str:
    """Aplica un contacto remoto. Devuelve 'echo' | 'applied' | 'unmatched'."""
    link = await session.scalar(select(ExternalLink).where(
        ExternalLink.connection_id == conn.id, ExternalLink.remote_type == contact_remote_type(conn),
        ExternalLink.remote_id == record.id))
    if not link:
        return "unmatched"  # solo se actualizan contactos que ya existen en ambos lados
    pushable = {m.remote_property for m in mappings if m.direction in ("push", "both")}
    if payload_hash({p: record.properties.get(p) for p in pushable}) == link.sync_hash:
        return "echo"
    contact = await session.get(Contact, link.local_id)
    if not contact:
        return "unmatched"
    defs = await fields_by_key(session, conn.organization_id)
    values = {m.local_field: backward(record.properties.get(m.remote_property), m.transform)
              for m in mappings if m.direction in ("pull", "both") and m.remote_property in record.properties
              and not attribution_fields.is_attribution(m.local_field)}
    if "first_name" in values or "last_name" in values:
        first, last = split_name(contact.name)
        name = " ".join(x for x in (values.pop("first_name", first), values.pop("last_name", last)) if x).strip()
        values["name"] = name or contact.name
    for local, value in values.items():
        if local in ("name", "email", "notes", "memory"):
            set_native(session, contact, local, (str(value).strip() or None) if value is not None else None, "crm")
        elif local == "stage" and value in STAGES:
            set_native(session, contact, "stage", value, "crm")
        elif local.startswith("custom:") and local.split(":", 1)[1] in defs:
            field = defs[local.split(":", 1)[1]]
            try:
                await set_custom(session, contact, field, coerce(field, value), "crm")
            except ValueError:
                continue
    await session.flush()
    # El nuevo estado local ya coincide con el CRM: se registra para que no vuelva como eco
    link.sync_hash = payload_hash(await contact_push_props(session, conn, contact))
    link.remote_updated_at, link.last_synced_at = record.updated_at, utcnow()
    return "applied"


async def apply_remote_deal(session: AsyncSession, conn: IntegrationConnection, record: RemoteRecord,
                            mappings: list[IntegrationMapping]) -> str:
    link = await session.scalar(select(ExternalLink).where(
        ExternalLink.connection_id == conn.id, ExternalLink.remote_type == deal_remote_type(conn),
        ExternalLink.remote_id == record.id))
    if not link:
        return "unmatched"
    pushable = {m.remote_property for m in mappings if m.direction in ("push", "both")}
    if payload_hash({p: record.properties.get(p) for p in pushable}) == link.sync_hash:
        return "echo"
    deal = await session.get(Deal, link.local_id)
    if not deal:
        return "unmatched"
    for m in mappings:
        if (m.direction not in ("pull", "both") or m.remote_property not in record.properties
                or attribution_fields.is_attribution(m.local_field)):
            continue
        value = backward(record.properties[m.remote_property], m.transform)
        if m.local_field == "deal.name" and value:
            deal.name = str(value)
        elif m.local_field == "deal.amount":
            try:
                deal.amount = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                pass
    await session.flush()
    link.sync_hash = payload_hash(await deal_push_props(session, conn, deal))
    link.remote_updated_at, link.last_synced_at = record.updated_at, utcnow()
    return "applied"


async def pull_changes(session: AsyncSession, conn: IntegrationConnection, adapter: CRMAdapter) -> dict:
    settings = dict(conn.settings or {})
    since = _parse_cursor(settings.get("pull_cursor")) or (conn.created_at or utcnow()) - timedelta(minutes=1)
    stats = {"applied": 0, "echo": 0, "unmatched": 0}
    newest = since
    for obj, fetch, apply in (("contact", adapter.changed_contacts, apply_remote_contact),
                              ("deal", adapter.changed_deals, apply_remote_deal)):
        mappings = await mappings_for(session, conn.id, obj)
        props = sorted({m.remote_property for m in mappings})
        if not props:
            continue
        for record in await fetch(since, props):
            stats[await apply(session, conn, record, mappings)] += 1
            if record.updated_at and record.updated_at > newest:
                newest = record.updated_at
    settings["pull_cursor"] = newest.isoformat()
    settings["last_pull_at"] = utcnow().isoformat()
    conn.settings = settings
    await session.commit()
    return stats


# --- Orquestación ---------------------------------------------------------------
async def sync_connection(conn_id: int, force_pull: bool = False) -> dict:
    """Ciclo completo de una conexión: escanear → enviar cola → traer cambios."""
    async with _locks[conn_id], SessionLocal() as session:
        conn = await session.get(IntegrationConnection, conn_id)
        if not conn or conn.status != "connected":
            return {"skipped": True}
        result = {"queued": await scan_changes(session, conn)}
        result["push"] = await process_outbox(session, conn)
        await session.refresh(conn)
        if conn.status != "connected":
            return result
        last_pull = _parse_cursor((conn.settings or {}).get("last_pull_at"))
        if force_pull or not last_pull or utcnow() - last_pull >= PULL_EVERY:
            try:
                adapter = await adapter_for(session, conn)
                try:
                    result["pull"] = await pull_changes(session, conn, adapter)
                except TokenRevoked:
                    adapter = await adapter_for(session, conn, force_refresh=True)
                    result["pull"] = await pull_changes(session, conn, adapter)
            except CRMError as e:
                await session.rollback()
                conn = await session.get(IntegrationConnection, conn_id)
                conn.last_error = f"Lectura de cambios: {e}"[:2000]
                if isinstance(e, TokenRevoked) or e.status == 401:
                    conn.status = "error"
                    await _alert(session, conn, f"{conn.provider.title()}: se perdió la conexión", str(e))
                result["pull_error"] = str(e)
        conn.last_sync_at = utcnow()
        if "pull_error" not in result and result["push"]["failed"] == 0:
            conn.last_error = conn.last_error if conn.status != "connected" else None
        await session.commit()
        return result


async def crm_loop() -> None:
    """Worker: cada 30 s sincroniza todas las conexiones activas de todas las organizaciones."""
    while True:
        await asyncio.sleep(30)
        try:
            async with SessionLocal() as session:
                rows = [(r[0], r[1]) for r in (await session.execute(
                select(IntegrationConnection.id, IntegrationConnection.organization_id, IntegrationConnection.provider,
                       IntegrationConnection.settings)
                .where(IntegrationConnection.status == "connected", IntegrationConnection.sync_enabled,
                       IntegrationConnection.provider.in_(CRM_PROVIDERS)))).all()
                    if r[2] != "custom" or (r[3] or {}).get("crm_push")]
            ids = [r[0] for r in rows]
            from app import jobs

            if jobs.enabled():  # una conexión por trabajo (cola "crm"): en paralelo y con reintentos de la cola
                await jobs.schedule("crm.sync_connection", [({"connection_id": i}, f"crm:{i}", o) for i, o in rows])
                continue
            for conn_id in ids:
                try:
                    await sync_connection(conn_id)
                except Exception:
                    log.exception("Falló la sincronización CRM de la conexión %s", conn_id)
        except Exception:
            log.exception("Falló el ciclo de sincronización CRM")

