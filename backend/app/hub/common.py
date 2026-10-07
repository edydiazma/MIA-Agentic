"""Piezas compartidas del hub: conexiones y secretos, enlace de clientes, pedidos normalizados y bitácora de
corridas (connector_runs)."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.golden import normalize as gn
from app.models import (
    Attribution,
    Contact,
    ContactKey,
    ConnectorRun,
    Conversation,
    ExternalOrder,
    IntegrationConnection,
    InteractionProduct,
    Product,
    utcnow,
)
from app.secrets_vault import get_secret, put_secret

log = logging.getLogger(__name__)

COMMERCE = ("shopify", "woocommerce", "vtex")
CALENDARS = ("google_calendar", "microsoft_calendar")
ORDER_STATUSES = ("pending", "paid", "fulfilled", "cancelled", "refunded")
LABELS = {"shopify": "Shopify", "woocommerce": "WooCommerce", "vtex": "VTEX", "google_calendar": "Google Calendar",
          "microsoft_calendar": "Outlook / Microsoft 365", "zoho": "Zoho CRM", "odoo": "Odoo", "custom": "Conector propio",
          "bigquery": "BigQuery"}


# --- Conexiones y secretos ---------------------------------------------------------------------------------
async def connection(session: AsyncSession, org: int, conn_id: int, providers: tuple[str, ...] | None = None
                     ) -> IntegrationConnection | None:
    conn = await session.get(IntegrationConnection, conn_id)
    if not conn or conn.organization_id != org or (providers and conn.provider not in providers):
        return None
    return conn


async def set_secret(session: AsyncSession, conn: IntegrationConnection, name: str, value: str | None) -> None:
    """Secretos adicionales de la conexión (clave de API, secreto de webhook…) en Vault; en settings solo el id."""
    if value is None:
        return
    s = dict(conn.settings or {})
    ids = dict(s.get("secret_ids") or {})
    ids[name] = await put_secret(session, value, f"integration:{conn.id}:{name}", ids.get(name))
    s["secret_ids"] = ids
    conn.settings = s


async def secret(session: AsyncSession, conn: IntegrationConnection, name: str) -> str | None:
    if name == "access_token":
        return await get_secret(session, conn.access_token_secret_id)
    if name == "refresh_token":
        return await get_secret(session, conn.refresh_token_secret_id)
    sid = ((conn.settings or {}).get("secret_ids") or {}).get(name)
    return await get_secret(session, sid) if sid else None


# --- Bitácora -----------------------------------------------------------------------------------------------
async def start_run(session: AsyncSession, conn: IntegrationConnection, entity: str, direction: str,
                    cursor: str | None = None) -> ConnectorRun:
    run = ConnectorRun(organization_id=conn.organization_id, connection_id=conn.id, entity=entity,
                       direction=direction, cursor_before=cursor)
    session.add(run)
    await session.flush()
    return run


def finish_run(run: ConnectorRun, error: str | None = None, cursor: str | None = None) -> None:
    run.finished_at = utcnow()
    run.cursor_after = cursor or run.cursor_after
    if error:
        run.error = error[:2000]
        run.status = "partial" if (run.created or run.updated) else "failed"
    else:
        run.status = "partial" if run.failed else "succeeded"


def note_error(run: ConnectorRun, ref: str, error: Exception | str) -> None:
    run.failed += 1
    errs = list(run.sample_errors or [])
    if len(errs) < 10:
        errs.append({"ref": str(ref)[:100], "error": str(error)[:300]})
        run.sample_errors = errs


# --- Clientes ----------------------------------------------------------------------------------------------
async def match_contact(session: AsyncSession, org: int, email: str | None, phone: str | None,
                        country: str = "CO") -> Contact | None:
    """Cliente existente por teléfono (WhatsApp o llave maestra) o correo (ficha o llave maestra)."""
    p = gn.phone(phone, country) if phone else None
    e = gn.email(email) if email else None
    conds = []
    if p:
        conds.append(Contact.wa_id == p.value)
    if e:
        conds.append(func.lower(Contact.email) == e.value)
    if conds:
        found = (await session.scalars(select(Contact).where(Contact.organization_id == org, or_(*conds))
                                       .order_by(Contact.id).limit(1))).first()
        if found:
            return found
    keys = []
    if p:
        keys.append((ContactKey.key_type == "phone") & (ContactKey.value_normalized == p.value))
    if e:
        keys.append((ContactKey.key_type == "email") & (ContactKey.value_normalized == e.value))
    if not keys:
        return None
    cid = await session.scalar(select(ContactKey.contact_id).where(
        ContactKey.organization_id == org, ContactKey.status == "active", or_(*keys))
        .order_by(ContactKey.verified.desc(), ContactKey.id).limit(1))
    return await session.get(Contact, cid) if cid else None


async def find_attribution(session: AsyncSession, contact_id: int, placed_at: datetime | None,
                           window_days: int) -> Attribution | None:
    """Conversación atribuida que antecede la compra (última dentro de la ventana)."""
    when = placed_at or utcnow()
    return (await session.scalars(select(Attribution).where(
        Attribution.contact_id == contact_id, Attribution.created_at <= when,
        Attribution.created_at >= when - timedelta(days=window_days))
        .order_by(Attribution.created_at.desc()).limit(1))).first()


# --- Catálogo y pedidos --------------------------------------------------------------------------------------
async def upsert_product(session: AsyncSession, conn: IntegrationConnection, item: dict) -> str:
    """item: {sku, name, description?, price?, currency?, stock?, image_url?, url?, available?, category?, brand?}.
    Devuelve "created" | "updated" | "skipped"."""
    sku = str(item.get("sku") or "").strip()
    if not sku or not item.get("name"):
        return "skipped"
    org = conn.organization_id
    p = await session.scalar(select(Product).where(Product.organization_id == org, Product.sku == sku))
    created = p is None
    if created:
        p = Product(organization_id=org, sku=sku, name=str(item["name"])[:500], source="api")
        session.add(p)
    p.name = str(item["name"])[:500]
    for k in ("description", "category", "brand", "url", "image_url"):
        if item.get(k) is not None:
            setattr(p, k, item[k])
    if item.get("price") is not None:
        p.price = max(0.0, float(item["price"]))
    if item.get("currency"):
        p.currency = str(item["currency"])[:3].upper()
    if item.get("stock") is not None:
        p.stock = int(item["stock"])
    p.available = bool(item.get("available", True)) and (p.stock is None or p.stock > 0)
    p.attributes = {**(p.attributes or {}), "store": {"provider": conn.provider, "connection_id": conn.id,
                                                      "label": conn.label, "external_id": item.get("external_id")}}
    p.updated_at = utcnow()
    await session.flush()
    return "created" if created else "updated"


async def upsert_order(session: AsyncSession, conn: IntegrationConnection, o: dict) -> str:
    """o (normalizado): {external_id, order_number, status (ORDER_STATUSES), status_raw, total, currency, items:
    [{sku, name, quantity, price}], customer: {email, phone, name}, placed_at, raw}. Enlaza cliente y atribución;
    al pasar a pagado registra los productos como comprados. Devuelve "created" | "updated" | "skipped"."""
    ext_id = str(o.get("external_id") or "").strip()
    if not ext_id:
        return "skipped"
    org = conn.organization_id
    s = conn.settings or {}
    row = await session.scalar(select(ExternalOrder).where(ExternalOrder.connection_id == conn.id,
                                                           ExternalOrder.external_id == ext_id))
    created = row is None
    was_paid = (not created) and row.status in ("paid", "fulfilled")
    if created:
        row = ExternalOrder(organization_id=org, connection_id=conn.id, external_id=ext_id, status="pending")
        session.add(row)
    status = o.get("status") if o.get("status") in ORDER_STATUSES else "pending"
    row.order_number = o.get("order_number") or row.order_number
    row.status, row.status_raw = status, o.get("status_raw")
    row.total = o.get("total") if o.get("total") is not None else row.total
    row.currency = (o.get("currency") or row.currency or "")[:3].upper() or None
    row.items = o.get("items") or []
    row.customer = o.get("customer") or {}
    row.placed_at = o.get("placed_at") or row.placed_at
    row.raw = o.get("raw")
    row.updated_at = utcnow()
    if row.contact_id is None:
        cust = row.customer or {}
        contact = await match_contact(session, org, cust.get("email"), cust.get("phone"), s.get("country", "CO"))
        if contact:
            row.contact_id = contact.id
    if row.contact_id and row.attribution_id is None:
        attr = await find_attribution(session, row.contact_id, row.placed_at, int(s.get("attribution_days", 30)))
        if attr:
            row.attribution_id = attr.id
    await session.flush()
    if row.contact_id and status in ("paid", "fulfilled") and not was_paid:
        await _record_purchase(session, row)
    return "created" if created else "updated"


async def _record_purchase(session: AsyncSession, order: ExternalOrder) -> None:
    """Productos del pedido como comprados (cliente 360 y demanda por producto)."""
    conv_id = None
    if order.attribution_id:
        conv_id = await session.scalar(select(Attribution.conversation_id).where(Attribution.id == order.attribution_id))
    if conv_id is None:
        conv_id = await session.scalar(select(Contact.last_conversation_id).where(Contact.id == order.contact_id))
    conv = await session.get(Conversation, conv_id) if conv_id else None
    from app.interaction_products import add_product, find_product

    for it in order.items or []:
        sku, name = str(it.get("sku") or "").strip(), (it.get("name") or "").strip()
        qty, price = it.get("quantity"), it.get("price")
        try:
            if conv is not None:
                await add_product(session, conv, stage="purchased", source="import", external_ref=sku or None,
                                  name=name or sku or None, quantity=qty, unit_price=price, currency=order.currency)
            else:
                product = await find_product(session, order.organization_id, sku) if sku else None
                session.add(InteractionProduct(
                    organization_id=order.organization_id, contact_id=order.contact_id, conversation_id=None,
                    product_id=product.id if product else None, external_ref=None if product else (sku or None),
                    name=(product.name if product else (name or sku or "Producto")), stage="purchased",
                    quantity=qty, unit_price=price, currency=order.currency, source="import"))
        except Exception as e:  # noqa: BLE001 — un ítem raro no frena el pedido
            log.warning("Pedido %s: no se registró el ítem %s: %s", order.external_id, sku or name, e)
    await session.flush()


def money(v) -> float | None:
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        from datetime import UTC

        d = d.replace(tzinfo=UTC)
    return d
