"""Productos por interacción: lo que el cliente mencionó, cotizó o compró en una conversación.

El producto puede ser del catálogo (product_id) o de cualquier sistema de la empresa (external_ref: SKU, código
del ERP/CRM). Lo registran el asesor, la IA (análisis de la conversación), un flujo (bloque register_product), la
API pública o un pedido de WhatsApp. Un mismo producto por conversación y etapa no se duplica.
docs/data-model.md §14
"""

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Conversation, InteractionProduct, Product

STAGES = ("mentioned", "interested", "quoted", "purchased", "not_interested")
SOURCES = ("ai", "agent", "flow", "api", "whatsapp_order", "catalog_message", "import")
AI_MIN_CONFIDENCE = 0.6


class ProductError(ValueError):
    pass


def product_brief(p: Product | None) -> dict | None:
    if p is None:
        return None
    return {"id": p.id, "sku": p.sku, "name": p.name, "price": float(p.price) if p.price is not None else None,
            "currency": p.currency, "image_url": p.image_url}


async def item_out(session: AsyncSession, row: InteractionProduct) -> dict:
    return (await items_out(session, [row]))[0]


async def items_out(session: AsyncSession, rows: list[InteractionProduct]) -> list[dict]:
    pids = {r.product_id for r in rows if r.product_id}
    products = {p.id: p for p in (await session.scalars(select(Product).where(Product.id.in_(pids)))).all()} \
        if pids else {}
    aids = {r.created_by for r in rows if r.created_by}
    agents = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.id.in_(aids)))).all()) if aids else {}
    return [{
        "id": r.id, "contact_id": r.contact_id, "conversation_id": r.conversation_id,
        "product": product_brief(products.get(r.product_id)), "external_ref": r.external_ref, "name": r.name,
        "category": r.category, "stage": r.stage, "quantity": float(r.quantity) if r.quantity is not None else None,
        "unit_price": float(r.unit_price) if r.unit_price is not None else None, "currency": r.currency,
        "source": r.source, "confidence": r.confidence, "created_at": r.created_at,
        "created_by": {"id": r.created_by, "name": agents[r.created_by]} if r.created_by in agents else None,
    } for r in rows]


async def find_product(session: AsyncSession, org: int, ref: str | None) -> Product | None:
    """Producto del catálogo por id, SKU o nombre exacto (sin distinguir mayúsculas)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    conds = [Product.sku == ref, func.lower(Product.name) == ref.lower()]
    if ref.isdigit():
        conds.append(Product.id == int(ref))
    return (await session.scalars(select(Product).where(Product.organization_id == org, or_(*conds))
                                  .order_by(Product.id).limit(1))).first()


async def add_product(session: AsyncSession, conv: Conversation, *, stage: str, source: str,
                      product_id: int | None = None, external_ref: str | None = None, name: str | None = None,
                      quantity: float | None = None, unit_price: float | None = None, currency: str | None = None,
                      category: str | None = None, confidence: float | None = None, message_id: int | None = None,
                      created_by: int | None = None) -> tuple[InteractionProduct, bool]:
    """Registra el producto en la conversación. Devuelve (fila, creada); si ya existía, la existente."""
    if stage not in STAGES:
        raise ProductError(f"Etapa inválida: {', '.join(STAGES)}")
    if source not in SOURCES:
        raise ProductError("Origen inválido")
    org = conv.organization_id
    product = None
    if product_id is not None:
        product = await session.get(Product, product_id)
        if not product or product.organization_id != org:
            raise ProductError("Producto no encontrado en el catálogo")
    elif external_ref or name:
        # Si la referencia o el nombre coinciden con el catálogo, se vincula
        product = await find_product(session, org, external_ref) or await find_product(session, org, name)
    label = (product.name if product else (name or external_ref or "")).strip()
    if not label:
        raise ProductError("Indica un producto del catálogo, una referencia externa o un nombre")
    key_conds = [InteractionProduct.conversation_id == conv.id, InteractionProduct.stage == stage]
    if product:
        key_conds.append(InteractionProduct.product_id == product.id)
    elif external_ref:
        key_conds += [InteractionProduct.product_id.is_(None), InteractionProduct.external_ref == external_ref]
    else:
        key_conds += [InteractionProduct.product_id.is_(None), InteractionProduct.external_ref.is_(None),
                      func.lower(InteractionProduct.name) == label.lower()]
    existing = (await session.scalars(select(InteractionProduct).where(*key_conds).limit(1))).first()
    if existing:
        return existing, False
    row = InteractionProduct(
        organization_id=org, contact_id=conv.contact_id, conversation_id=conv.id, message_id=message_id,
        product_id=product.id if product else None, external_ref=external_ref or None, name=label,
        category=category or (product.category if product else None), stage=stage, quantity=quantity,
        unit_price=unit_price if unit_price is not None else (
            float(product.sale_price or product.price) if product and (product.sale_price or product.price) else None),
        currency=currency or (product.currency if product else None), source=source, confidence=confidence,
        created_by=created_by)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:  # otra petición lo registró al mismo tiempo
        existing = (await session.scalars(select(InteractionProduct).where(*key_conds).limit(1))).first()
        if existing:
            return existing, False
        raise
    return row, True


async def record_order(session: AsyncSession, conv: Conversation, order: dict, message_id: int | None) -> int:
    """Pedido de WhatsApp (mensaje type=order): cada ítem queda como comprado, enlazado por SKU."""
    n = 0
    for item in order.get("product_items") or []:
        sku = str(item.get("product_retailer_id") or "").strip()
        if not sku:
            continue
        product = await find_product(session, conv.organization_id, sku)
        await add_product(session, conv, stage="purchased", source="whatsapp_order",
                          product_id=product.id if product else None, external_ref=None if product else sku,
                          name=None if product else sku, quantity=_num(item.get("quantity")),
                          unit_price=_num(item.get("item_price")), currency=item.get("currency"),
                          message_id=message_id)
        n += 1
    return n


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def record_ai_products(session: AsyncSession, conv: Conversation, detected: list[dict]) -> int:
    """Productos detectados por la IA en el análisis de la conversación (confianza ≥ 0.6)."""
    n = 0
    for d in detected or []:
        try:
            confidence = float(d.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        name = str(d.get("name") or "").strip()
        stage = d.get("stage") if d.get("stage") in STAGES else "interested"
        if confidence < AI_MIN_CONFIDENCE or not name:
            continue
        product = await find_product(session, conv.organization_id, d.get("catalog_sku_or_null")) \
            if d.get("catalog_sku_or_null") else None
        try:
            _, created = await add_product(session, conv, stage=stage, source="ai",
                                           product_id=product.id if product else None,
                                           name=None if product else name, confidence=confidence)
        except ProductError:
            continue
        n += int(created)
    return n
