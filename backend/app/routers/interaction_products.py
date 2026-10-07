"""Productos por interacción (ficha del cliente y conversación) y búsqueda en el catálogo. docs/data-model.md §14"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog
from app.auth import current_agent
from app.db import get_session
from app.interaction_products import STAGES, ProductError, add_product, item_out, items_out, product_brief
from app.models import Agent, Contact, Conversation, InteractionProduct, Typification

router = APIRouter(prefix="/api", tags=["interaction-products"])


class ProductIn(BaseModel):
    product_id: int | None = None
    external_ref: str | None = None
    name: str | None = None
    stage: str = "interested"
    quantity: float | None = None
    unit_price: float | None = None
    currency: str | None = None


class ProductPatch(BaseModel):
    stage: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    currency: str | None = None
    external_ref: str | None = None


async def _conv(session: AsyncSession, conv_id: int, agent: Agent) -> Conversation:
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.organization_id != agent.organization_id:
        raise HTTPException(404, "Conversación no encontrada")
    return conv


async def _row(session: AsyncSession, row_id: int, agent: Agent) -> InteractionProduct:
    row = await session.get(InteractionProduct, row_id)
    if not row or row.organization_id != agent.organization_id:
        raise HTTPException(404, "Producto no encontrado")
    return row


def _order():
    return (InteractionProduct.created_at.desc(), InteractionProduct.id.desc())


@router.get("/contacts/{contact_id}/products")
async def contact_products(contact_id: int, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    contact = await session.get(Contact, contact_id)
    if not contact or contact.organization_id != agent.organization_id:
        raise HTTPException(404, "Contacto no encontrado")
    rows = (await session.scalars(select(InteractionProduct).where(InteractionProduct.contact_id == contact_id)
                                  .order_by(*_order()))).all()
    return await items_out(session, list(rows))


@router.get("/conversations/{conv_id}/products")
async def conversation_products(conv_id: int, agent: Agent = Depends(current_agent),
                                session: AsyncSession = Depends(get_session)):
    await _conv(session, conv_id, agent)
    rows = (await session.scalars(select(InteractionProduct).where(InteractionProduct.conversation_id == conv_id)
                                  .order_by(*_order()))).all()
    return await items_out(session, list(rows))


@router.post("/conversations/{conv_id}/products")
async def add_conversation_product(conv_id: int, body: ProductIn, response: Response,
                                   agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, conv_id, agent)
    try:
        row, created = await add_product(session, conv, stage=body.stage, source="agent", product_id=body.product_id,
                                         external_ref=(body.external_ref or "").strip() or None,
                                         name=(body.name or "").strip() or None, quantity=body.quantity,
                                         unit_price=body.unit_price, currency=body.currency, created_by=agent.id)
    except ProductError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    response.status_code = 201 if created else 200
    return await item_out(session, row)


@router.patch("/interaction-products/{row_id}")
async def update_product(row_id: int, body: ProductPatch, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    row = await _row(session, row_id, agent)
    data = body.model_dump(exclude_unset=True)
    if "stage" in data and data["stage"] not in STAGES:
        raise HTTPException(422, f"Etapa inválida: {', '.join(STAGES)}")
    for k, v in data.items():
        setattr(row, k, (v.strip() or None) if isinstance(v, str) and k == "external_ref" else v)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(409, "Ese producto ya está registrado en esta conversación con esa etapa") from e
    return await item_out(session, row)


@router.delete("/interaction-products/{row_id}")
async def delete_product(row_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    row = await _row(session, row_id, agent)
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/products/search")
async def search_products(q: str = "", limit: int = 10, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    rows = await catalog.search(session, agent.organization_id, q, limit=min(max(limit, 1), 50))
    return [product_brief(p) for p in rows]


@router.get("/typifications")
async def list_typifications(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Tipificaciones de la empresa (filtros y columnas de la lista de clientes)."""
    rows = (await session.scalars(select(Typification).where(Typification.organization_id == agent.organization_id)
                                  .order_by(Typification.position, Typification.name))).all()
    return [{"id": t.id, "name": t.name, "is_success": t.is_success, "active": t.is_active, "section": t.section,
             "keyword": t.keyword, "required_fields": list(t.required_fields or [])} for t in rows]
