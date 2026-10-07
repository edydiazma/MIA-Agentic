"""Tiendas: Shopify (Admin GraphQL), WooCommerce (REST v3) y VTEX (Catalog + OMS).

Cada cliente devuelve productos y pedidos NORMALIZADOS (ver common.upsert_product / upsert_order) y verifica la
firma de sus webhooks. `sync_store(connection_id)` trae catálogo y pedidos cambiados desde el último cursor.
"""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.hub import common, http
from app.hub.http import HubError
from app.models import IntegrationConnection, utcnow

MAX_PAGES = 50  # tope por corrida (el cursor continúa en la siguiente)


def _hmac_b64(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# --- Shopify -------------------------------------------------------------------------------------------------
SHOPIFY_FINANCIAL = {"PAID": "paid", "PARTIALLY_PAID": "paid", "REFUNDED": "refunded", "PARTIALLY_REFUNDED": "paid",
                     "VOIDED": "cancelled", "PENDING": "pending", "AUTHORIZED": "pending", "EXPIRED": "cancelled"}

ORDERS_QUERY = """query($first: Int!, $after: String, $q: String) {
  orders(first: $first, after: $after, query: $q, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { id name createdAt updatedAt cancelledAt displayFinancialStatus displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      customer { email phone firstName lastName } email phone
      lineItems(first: 100) { nodes { sku title quantity originalUnitPriceSet { shopMoney { amount } } } } }
  }
}"""

PRODUCTS_QUERY = """query($first: Int!, $after: String, $q: String) {
  products(first: $first, after: $after, query: $q, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { id title descriptionHtml vendor productType status onlineStoreUrl updatedAt
      featuredImage { url }
      variants(first: 100) { nodes { id sku title price inventoryQuantity availableForSale } } }
  }
}"""


class Shopify:
    def __init__(self, shop: str, token: str, api_version: str | None = None):
        shop = shop.strip().lower().removeprefix("https://").rstrip("/")
        if not shop.endswith(".myshopify.com"):
            shop = f"{shop}.myshopify.com"
        self.shop = shop
        self.url = f"https://{shop}/admin/api/{api_version or get_settings().shopify_api_version}/graphql.json"
        self.headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    async def _gql(self, query: str, variables: dict) -> dict:
        r = http.check(await http.send("POST", self.url, headers=self.headers,
                                       json_body={"query": query, "variables": variables}), "Shopify")
        data = r.json()
        if data.get("errors"):
            msg = str(data["errors"])[:400]
            throttled = "THROTTLED" in msg
            raise HubError(f"Shopify: {msg}", retryable=throttled, status=429 if throttled else 400)
        return data["data"]

    async def shop_info(self) -> dict:
        return (await self._gql("{ shop { name currencyCode myshopifyDomain } }", {}))["shop"]

    async def _paged(self, query: str, key: str, since: datetime | None):
        q = f"updated_at:>'{since.isoformat()}'" if since else None
        after = None
        for _ in range(MAX_PAGES):
            page = (await self._gql(query, {"first": 50, "after": after, "q": q}))[key]
            for node in page["nodes"]:
                yield node
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    async def products(self, since: datetime | None):
        async for p in self._paged(PRODUCTS_QUERY, "products", since):
            for v in (p.get("variants") or {}).get("nodes") or []:
                yield {"sku": v.get("sku") or v["id"].rsplit("/", 1)[-1],
                       "name": p["title"] + ("" if v.get("title") in (None, "Default Title") else f" — {v['title']}"),
                       "description": p.get("descriptionHtml"), "brand": p.get("vendor"),
                       "category": p.get("productType"), "price": common.money(v.get("price")),
                       "stock": v.get("inventoryQuantity"), "url": p.get("onlineStoreUrl"),
                       "image_url": (p.get("featuredImage") or {}).get("url"),
                       "available": p.get("status") == "ACTIVE" and v.get("availableForSale", True),
                       "external_id": v["id"]}

    async def orders(self, since: datetime | None):
        async for o in self._paged(ORDERS_QUERY, "orders", since):
            yield normalize_shopify_order(o)

    async def order(self, gid: str) -> dict | None:
        q = ORDERS_QUERY.replace("orders(first: $first, after: $after, query: $q, sortKey: UPDATED_AT)",
                                 "orders(first: 1, query: $q)")
        nodes = (await self._gql(q, {"first": 1, "after": None, "q": f"id:{gid.rsplit('/', 1)[-1]}"}))["orders"]["nodes"]
        return normalize_shopify_order(nodes[0]) if nodes else None


def normalize_shopify_order(o: dict) -> dict:
    money = (o.get("totalPriceSet") or {}).get("shopMoney") or {}
    status = "cancelled" if o.get("cancelledAt") else SHOPIFY_FINANCIAL.get(o.get("displayFinancialStatus") or "", "pending")
    if status == "paid" and o.get("displayFulfillmentStatus") == "FULFILLED":
        status = "fulfilled"
    cust = o.get("customer") or {}
    return {"external_id": o["id"], "order_number": o.get("name"), "status": status,
            "status_raw": f"{o.get('displayFinancialStatus')}/{o.get('displayFulfillmentStatus')}",
            "total": common.money(money.get("amount")), "currency": money.get("currencyCode"),
            "items": [{"sku": li.get("sku"), "name": li.get("title"), "quantity": li.get("quantity"),
                       "price": common.money(((li.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount"))}
                      for li in (o.get("lineItems") or {}).get("nodes") or []],
            "customer": {"email": cust.get("email") or o.get("email"), "phone": cust.get("phone") or o.get("phone"),
                         "name": " ".join(x for x in (cust.get("firstName"), cust.get("lastName")) if x) or None},
            "placed_at": common.parse_dt(o.get("createdAt")), "raw": {"id": o["id"], "updatedAt": o.get("updatedAt")}}


def normalize_shopify_webhook(body: dict) -> dict:
    """Webhook REST (orders/create|updated): mismo resultado que el pedido de GraphQL."""
    fin = (body.get("financial_status") or "").upper()
    status = "cancelled" if body.get("cancelled_at") else SHOPIFY_FINANCIAL.get(fin, "pending")
    if status == "paid" and (body.get("fulfillment_status") or "") == "fulfilled":
        status = "fulfilled"
    cust = body.get("customer") or {}
    return {"external_id": body.get("admin_graphql_api_id") or f"gid://shopify/Order/{body.get('id')}",
            "order_number": body.get("name"), "status": status,
            "status_raw": f"{body.get('financial_status')}/{body.get('fulfillment_status')}",
            "total": common.money(body.get("total_price")), "currency": body.get("currency"),
            "items": [{"sku": li.get("sku"), "name": li.get("title") or li.get("name"), "quantity": li.get("quantity"),
                       "price": common.money(li.get("price"))} for li in body.get("line_items") or []],
            "customer": {"email": body.get("email") or cust.get("email"), "phone": body.get("phone") or cust.get("phone"),
                         "name": " ".join(x for x in (cust.get("first_name"), cust.get("last_name")) if x) or None},
            "placed_at": common.parse_dt(body.get("created_at")), "raw": {"id": body.get("id"), "webhook": True}}


def verify_shopify(secret: str, body: bytes, header: str | None) -> bool:
    return bool(secret and header) and hmac.compare_digest(_hmac_b64(secret, body), header)


# --- WooCommerce ---------------------------------------------------------------------------------------------
WOO_STATUS = {"pending": "pending", "on-hold": "pending", "processing": "paid", "completed": "fulfilled",
              "cancelled": "cancelled", "failed": "cancelled", "refunded": "refunded", "checkout-draft": "pending"}


class WooCommerce:
    def __init__(self, url: str, key: str, secret: str):
        self.base = url.rstrip("/") + "/wp-json/wc/v3"
        if not self.base.startswith("https://"):
            raise HubError("La tienda debe usar https", retryable=False)
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    async def _get(self, path: str, params: dict) -> tuple[list, int]:
        r = http.check(await http.send("GET", f"{self.base}{path}", headers=self.headers, params=params), "WooCommerce")
        return r.json(), int(r.headers.get("X-WP-TotalPages", "1") or 1)

    async def ping(self) -> dict:
        r = http.check(await http.send("GET", f"{self.base}/system_status", headers=self.headers), "WooCommerce")
        return (r.json() or {}).get("environment") or {}

    async def _paged(self, path: str, since: datetime | None):
        params = {"per_page": 100, "orderby": "modified", "order": "asc"}
        if since:
            params["modified_after"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        for page in range(1, MAX_PAGES + 1):
            rows, pages = await self._get(path, {**params, "page": page})
            for row in rows:
                yield row
            if page >= pages or not rows:
                return

    async def products(self, since: datetime | None):
        async for p in self._paged("/products", since):
            yield {"sku": p.get("sku") or str(p["id"]), "name": p.get("name"), "description": p.get("short_description"),
                   "price": common.money(p.get("price")), "stock": p.get("stock_quantity"), "url": p.get("permalink"),
                   "image_url": ((p.get("images") or [{}])[0] or {}).get("src"),
                   "category": ((p.get("categories") or [{}])[0] or {}).get("name"),
                   "available": p.get("status") == "publish" and p.get("stock_status") != "outofstock",
                   "external_id": str(p["id"])}

    async def orders(self, since: datetime | None):
        async for o in self._paged("/orders", since):
            yield normalize_woo_order(o)


def normalize_woo_order(o: dict) -> dict:
    b = o.get("billing") or {}
    return {"external_id": str(o["id"]), "order_number": str(o.get("number") or o["id"]),
            "status": WOO_STATUS.get(o.get("status") or "", "pending"), "status_raw": o.get("status"),
            "total": common.money(o.get("total")), "currency": o.get("currency"),
            "items": [{"sku": li.get("sku"), "name": li.get("name"), "quantity": li.get("quantity"),
                       "price": common.money(li.get("price"))} for li in o.get("line_items") or []],
            "customer": {"email": b.get("email"), "phone": b.get("phone"),
                         "name": " ".join(x for x in (b.get("first_name"), b.get("last_name")) if x) or None},
            "placed_at": common.parse_dt(o.get("date_created_gmt") and o["date_created_gmt"] + "Z"),
            "raw": {"id": o["id"], "modified": o.get("date_modified_gmt")}}


def verify_woo(secret: str, body: bytes, header: str | None) -> bool:
    return bool(secret and header) and hmac.compare_digest(_hmac_b64(secret, body), header)


# --- VTEX ------------------------------------------------------------------------------------------------------
VTEX_STATUS = {"payment-pending": "pending", "waiting-for-sellers-confirmation": "pending", "order-created": "pending",
               "payment-approved": "paid", "ready-for-handling": "paid", "handling": "paid", "start-handling": "paid",
               "invoiced": "fulfilled", "invoice": "fulfilled", "canceled": "cancelled", "cancel": "cancelled",
               "window-to-cancel": "pending"}


class Vtex:
    def __init__(self, account: str, app_key: str, app_token: str, environment: str = "vtexcommercestable"):
        self.base = f"https://{account}.{environment}.com.br"
        self.headers = {"X-VTEX-API-AppKey": app_key, "X-VTEX-API-AppToken": app_token, "Accept": "application/json"}

    async def _get(self, path: str, params: dict | None = None):
        return http.check(await http.send("GET", f"{self.base}{path}", headers=self.headers, params=params), "VTEX").json()

    async def ping(self) -> dict:
        data = await self._get("/api/oms/pvt/orders", {"per_page": 1})
        return {"orders": (data.get("paging") or {}).get("total")}

    async def orders(self, since: datetime | None):
        since = since or (utcnow() - timedelta(days=30))
        frm = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to = utcnow().strftime("%Y-%m-%dT%H:%M:%S.999Z")
        for page in range(1, MAX_PAGES + 1):
            data = await self._get("/api/oms/pvt/orders", {"f_creationDate": f"creationDate:[{frm} TO {to}]",
                                                           "page": page, "per_page": 100})
            for row in data.get("list") or []:
                detail = await self.order(row["orderId"])
                if detail:
                    yield detail
            if page >= ((data.get("paging") or {}).get("pages") or 1):
                return

    async def order(self, order_id: str) -> dict | None:
        try:
            o = await self._get(f"/api/oms/pvt/orders/{order_id}")
        except HubError as e:
            if e.status == 404:
                return None
            raise
        return normalize_vtex_order(o)

    async def products(self, since: datetime | None):
        """Catálogo: ids de SKU paginados + detalle de cada SKU (precio desde la API de precios)."""
        for page in range(1, MAX_PAGES + 1):
            ids = await self._get("/api/catalog_system/pvt/sku/stockkeepingunitids", {"page": page, "pagesize": 100})
            if not ids:
                return
            for sku_id in ids:
                s = await self._get(f"/api/catalog_system/pvt/sku/stockkeepingunitbyid/{sku_id}")
                try:
                    price = (await self._get(f"/api/pricing/prices/{sku_id}")).get("basePrice")
                except HubError:
                    price = None
                yield {"sku": s.get("AlternateIds", {}).get("RefId") or str(sku_id),
                       "name": s.get("ProductName") or s.get("NameComplete") or s.get("SkuName"),
                       "description": s.get("ProductDescription"), "brand": s.get("BrandName"),
                       "price": common.money(price), "image_url": s.get("ImageUrl"),
                       "url": s.get("DetailUrl"), "available": bool(s.get("IsActive", True)), "external_id": str(sku_id)}
            if len(ids) < 100:
                return


def _vtex_email(v: str | None) -> str | None:
    """VTEX enmascara el correo como «correo-<id>.ct.vtex.com.br»: se recupera el original."""
    if not v:
        return None
    return v.rsplit("-", 1)[0] if v.endswith(".ct.vtex.com.br") and "-" in v else v


def normalize_vtex_order(o: dict) -> dict:
    c = o.get("clientProfileData") or {}
    status_raw = o.get("status") or ""
    return {"external_id": str(o["orderId"]), "order_number": o.get("sequence") or o.get("orderId"),
            "status": VTEX_STATUS.get(status_raw, "pending"), "status_raw": status_raw,
            "total": common.money((o.get("value") or 0) / 100), "currency": (o.get("storePreferencesData") or {}).get("currencyCode"),
            "items": [{"sku": it.get("refId") or it.get("sellerSku") or it.get("id"), "name": it.get("name"),
                       "quantity": it.get("quantity"), "price": common.money((it.get("price") or 0) / 100)}
                      for it in o.get("items") or []],
            "customer": {"email": _vtex_email(c.get("email")), "phone": c.get("phone"),
                         "name": " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x) or None},
            "placed_at": common.parse_dt(o.get("creationDate")), "raw": {"orderId": o["orderId"], "status": status_raw}}


def verify_vtex(secret: str, header: str | None) -> bool:
    """VTEX no firma el hook: se valida el encabezado secreto que configuramos en /api/orders/hook/config."""
    return bool(secret and header) and hmac.compare_digest(secret, header)


# --- Cliente de una conexión y sincronización -------------------------------------------------------------------
async def client_for(session: AsyncSession, conn: IntegrationConnection):
    s = conn.settings or {}
    if conn.provider == "shopify":
        token = await common.secret(session, conn, "access_token")
        if not token:
            raise HubError("La tienda Shopify no tiene token", retryable=False)
        return Shopify(s.get("shop") or conn.external_account_id or "", token)
    if conn.provider == "woocommerce":
        return WooCommerce(s.get("url") or conn.instance_url or "", await common.secret(session, conn, "consumer_key") or "",
                           await common.secret(session, conn, "consumer_secret") or "")
    if conn.provider == "vtex":
        return Vtex(s.get("account") or conn.external_account_id or "", await common.secret(session, conn, "app_key") or "",
                    await common.secret(session, conn, "app_token") or "", s.get("environment") or "vtexcommercestable")
    raise HubError(f"{conn.provider} no es una tienda", retryable=False)


async def sync_store(session: AsyncSession, conn: IntegrationConnection, entities: tuple[str, ...] = ("product", "order"),
                     full: bool = False) -> dict:
    """Trae productos y pedidos cambiados desde el último cursor (settings.cursors). Idempotente."""
    client = await client_for(session, conn)
    s = dict(conn.settings or {})
    cursors = dict(s.get("cursors") or {})
    out = {}
    for entity in entities:
        if entity == "product" and not s.get("sync_catalog", True):
            continue
        since = None if full else common.parse_dt(cursors.get(entity))
        started = utcnow()
        run = await common.start_run(session, conn, entity, "pull", cursors.get(entity))
        error = None
        try:
            stream = client.products(since) if entity == "product" else client.orders(since)
            async for item in stream:
                run.fetched += 1
                try:
                    async with session.begin_nested():
                        res = await (common.upsert_product(session, conn, item) if entity == "product"
                                     else common.upsert_order(session, conn, item))
                    setattr(run, res, getattr(run, res) + 1)
                except Exception as e:  # noqa: BLE001
                    common.note_error(run, item.get("sku") or item.get("external_id"), e)
            cursors[entity] = (started - timedelta(minutes=2)).isoformat()  # solapamiento: reloj del proveedor
        except HubError as e:
            error = str(e)
            if not e.retryable:
                conn.status, conn.last_error = "error", error[:500]
        common.finish_run(run, error, cursors.get(entity))
        out[entity] = {"fetched": run.fetched, "created": run.created, "updated": run.updated, "failed": run.failed,
                       "error": error}
    s["cursors"] = cursors
    conn.settings = s
    conn.last_sync_at = utcnow()
    if all(not v.get("error") for v in out.values()):
        conn.last_error = None
        if conn.status == "error":
            conn.status = "connected"
    await session.commit()
    return out
