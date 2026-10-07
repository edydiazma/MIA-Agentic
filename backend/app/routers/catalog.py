"""Catálogo de productos: CRUD, búsqueda, importación (CSV/Excel/JSON) y sincronización (Meta y feed externo)."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, CatalogSyncRun, Channel, Product
from app.schemas import UTCDateTime
from app.service import wa_client
from app.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/catalog", tags=["catalog"])
MAX_IMPORT = 20 * 1024 * 1024


class ProductIn(BaseModel):
    sku: str
    name: str
    description: str | None = None
    category: str | None = None
    brand: str | None = None
    price: float | None = Field(default=None, ge=0)
    sale_price: float | None = Field(default=None, ge=0)
    currency: str | None = None
    stock: int | None = None
    available: bool = True
    url: str | None = None
    image_url: str | None = None
    attributes: dict | None = None


class ProductOut(ProductIn):
    id: int
    currency: str
    source: str
    in_meta_catalog: bool
    updated_at: UTCDateTime


class RunOut(BaseModel):
    id: int
    source: str
    status: str
    stats: dict | None
    error: str | None
    started_at: UTCDateTime
    finished_at: UTCDateTime | None


def _out(p: Product) -> ProductOut:
    return ProductOut(id=p.id, sku=p.sku, name=p.name, description=p.description, category=p.category, brand=p.brand,
                      price=float(p.price) if p.price is not None else None,
                      sale_price=float(p.sale_price) if p.sale_price is not None else None, currency=p.currency,
                      stock=p.stock, available=p.available, url=p.url, image_url=p.image_url, attributes=p.attributes,
                      source=p.source, in_meta_catalog=p.in_meta_catalog, updated_at=p.updated_at)


async def _product(session: AsyncSession, pid: int, org: int) -> Product:
    p = await session.get(Product, pid)
    if not p or p.organization_id != org:
        raise HTTPException(404, "Producto no encontrado")
    return p


@router.get("/products")
async def list_products(
    q: str | None = None, category: str | None = None, available: bool | None = None,
    offset: int = 0, limit: int = Query(default=50, le=500),
    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session),
):
    stmt = select(Product).where(Product.organization_id == agent.organization_id)
    if category:
        stmt = stmt.where(Product.category.ilike(f"%{category}%"))
    if available is not None:
        stmt = stmt.where(Product.available.is_(available))
    if q:
        like = f"%{q}%"
        tsq = func.websearch_to_tsquery("spanish", q)
        stmt = stmt.where(or_(catalog.SEARCH.op("@@")(tsq), Product.name.ilike(like), Product.sku.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    categories = (await session.scalars(select(Product.category).where(
        Product.organization_id == agent.organization_id, Product.category.is_not(None))
        .distinct().order_by(Product.category))).all()
    rows = (await session.scalars(stmt.order_by(Product.name).offset(offset).limit(limit))).all()
    return {"total": total, "categories": categories, "items": [_out(p) for p in rows]}


@router.get("/search")
async def search(q: str = "", max_price: float | None = None, category: str | None = None,
                 limit: int = Query(default=5, le=50), agent: Agent = Depends(current_agent),
                 session: AsyncSession = Depends(get_session)):
    """Lo mismo que ve un agente de IA al buscar en el catálogo."""
    rows = await catalog.search(session, agent.organization_id, q, max_price, category, limit)
    return [{"product": _out(p), "as_seen_by_ai": catalog.describe(p)} for p in rows]


@router.post("/products", response_model=ProductOut)
async def create_product(body: ProductIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    sku = body.sku.strip()
    if not sku or not body.name.strip():
        raise HTTPException(422, "SKU y nombre son obligatorios")
    if await session.scalar(select(Product.id).where(Product.organization_id == agent.organization_id,
                                                     Product.sku == sku)):
        raise HTTPException(409, "Ya existe un producto con ese SKU")
    cfg = await get_setting(session, "catalog", agent.organization_id)
    data = body.model_dump()
    data["sku"], data["currency"] = sku, (body.currency or cfg["currency"]).upper()[:3]
    p = Product(organization_id=agent.organization_id, source="manual", **data)
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return _out(p)


@router.put("/products/{pid}", response_model=ProductOut)
async def update_product(pid: int, body: ProductIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    p = await _product(session, pid, agent.organization_id)
    if body.sku.strip() != p.sku:
        dup = await session.scalar(select(Product.id).where(Product.organization_id == agent.organization_id,
                                                            Product.sku == body.sku.strip()))
        if dup:
            raise HTTPException(409, "Ya existe un producto con ese SKU")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v.strip() if k == "sku" else (v.upper()[:3] if k == "currency" and v else v))
    await session.commit()
    await session.refresh(p)
    return _out(p)


@router.delete("/products/{pid}")
async def delete_product(pid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    p = await _product(session, pid, agent.organization_id)
    await session.delete(p)
    await session.commit()
    return {"ok": True}


@router.post("/import")
async def import_products(file: UploadFile = File(...), agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """CSV, Excel (.xlsx) o JSON. Columnas reconocidas: sku, nombre, precio, stock, categoria, marca, imagen...
    Las demás columnas quedan como atributos del producto (año, color, km...)."""
    content = await file.read()
    if len(content) > MAX_IMPORT:
        raise HTTPException(413, "Archivo demasiado grande (máx. 20 MB)")
    try:
        rows = catalog.parse_file(file.filename or "catalogo.csv", content)
    except Exception as e:
        raise HTTPException(422, f"No se pudo leer el archivo: {e}") from None
    if not rows:
        raise HTTPException(422, "El archivo no tiene filas")
    return await catalog.upsert(session, agent.organization_id, rows, "import", agent_id=agent.id)


@router.post("/sync/meta")
async def sync_meta(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    channel = await session.scalar(select(Channel).where(Channel.organization_id == agent.organization_id, Channel.provider == "whatsapp_cloud")
                                   .order_by(Channel.id).limit(1))
    if not channel:
        raise HTTPException(422, "Configura un número de WhatsApp primero")
    try:
        return await catalog.sync_meta(session, agent.organization_id, await wa_client(session, channel), agent.id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except Exception as e:
        raise HTTPException(502, f"Meta no respondió: {e}") from None


@router.post("/sync/feed")
async def sync_feed(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    try:
        return await catalog.sync_feed(session, agent.organization_id, agent.id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except Exception as e:
        raise HTTPException(502, f"No se pudo descargar el feed: {e}") from None


@router.get("/sync-runs", response_model=list[RunOut])
async def sync_runs(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(CatalogSyncRun).where(
        CatalogSyncRun.organization_id == agent.organization_id).order_by(CatalogSyncRun.id.desc()).limit(50))).all()
    return [RunOut(id=r.id, source=r.source, status=r.status, stats=r.stats, error=r.error, started_at=r.started_at,
                   finished_at=r.finished_at) for r in rows]


@router.get("/settings")
async def get_catalog_settings(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await get_setting(session, "catalog", agent.organization_id)


@router.put("/settings")
async def put_catalog_settings(body: dict, agent: Agent = Depends(require_admin),
                               session: AsyncSession = Depends(get_session)):
    if body.get("feed_url") and not str(body["feed_url"]).startswith("http"):
        raise HTTPException(422, "La URL del feed debe empezar con http(s)://")
    if body.get("feed_format") and body["feed_format"] not in ("csv", "json"):
        raise HTTPException(422, "Formato del feed: csv o json")
    body.pop("last_sync", None)
    return await set_setting(session, "catalog", body, org=agent.organization_id, agent_id=agent.id)
