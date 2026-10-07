"""Constructor de conectores propios (REST + mapeo) para DMS / ERP / sistemas de la empresa.

Una definición (connector_definitions) describe:
- auth: {type: none|api_key|bearer|basic|oauth2_client_credentials, header?, query_param?, token_url?, scopes?}
  Los secretos (api_key, username, password, client_id, client_secret, webhook_secret) van en Vault por conexión.
- endpoints: [{key, method, path, query, body, pagination: {type: none|page|offset|cursor|link, ...}, items_path}]
  Plantillas {{since}}, {{page}}, {{email}}, {{phone}}, {{id}} en path / query / body.
- mappings: {contact|product|order: {pull: {endpoint, id, updated?, fields: {local: "$.ruta" | {path, transforms,
  value?}}}, push?: {endpoint, id_path?}}, find_contact?: {endpoint}}
- webhooks: {secret_header, signature: hmac_sha256|token, encoding: hex|base64, entity: order|contact|product,
  items_path?}

Transformaciones: trim, lower, upper, phone_e164, digits, number, date, map:{a:b}, prefix:x, suffix:x,
default:x, concat:[paths], split:[sep, index].
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from urllib.parse import urlsplit
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.crm.base import CRMAdapter, CRMError
from app.golden import normalize as gn
from app.hub import common, http
from app.hub.http import HubError
from app.models import Contact, ConnectorDefinition, IntegrationConnection, utcnow

AUTH_TYPES = ("none", "api_key", "bearer", "basic", "oauth2_client_credentials")
PAGINATION = ("none", "page", "offset", "cursor", "link")
ENTITIES = ("contact", "product", "order")
SECRET_NAMES = ("api_key", "username", "password", "client_id", "client_secret", "webhook_secret")
MAX_PAGES = 50
_TPL = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_SEG = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")


# --- JSONPath (subconjunto) ----------------------------------------------------------------------------------
def jget(data, path: str | None):
    """`$.a.b[0].c`, `$.items[*].sku` (lista), `$` (todo). None si no existe."""
    if path in (None, "", "$"):
        return data
    p = path[2:] if path.startswith("$.") else path.lstrip("$")
    cur = [data]
    multi = False
    for name, idx in _SEG.findall(p):
        nxt = []
        for c in cur:
            if name:
                if isinstance(c, dict) and name in c:
                    nxt.append(c[name])
            elif idx == "*":
                multi = True
                if isinstance(c, list):
                    nxt.extend(c)
            elif isinstance(c, list) and int(idx) < len(c):
                nxt.append(c[int(idx)])
        cur = nxt
        if not cur:
            return [] if multi else None
    return cur if multi else cur[0]


def transform(value, steps: list, item=None, country: str = "CO"):
    for step in steps or []:
        op, arg = (next(iter(step.items())) if isinstance(step, dict) else (step, None))
        if op == "default":
            value = value if value not in (None, "") else arg
            continue
        if op == "concat":
            parts = [jget(item, p) if isinstance(p, str) and p.startswith("$") else p for p in (arg or [])]
            value = " ".join(str(x) for x in parts if x not in (None, ""))
            continue
        if value is None:
            continue
        if op == "trim":
            value = str(value).strip()
        elif op == "lower":
            value = str(value).lower()
        elif op == "upper":
            value = str(value).upper()
        elif op == "digits":
            value = re.sub(r"\D", "", str(value))
        elif op == "phone_e164":
            n = gn.phone(str(value), country)
            value = n.value if n else None
        elif op == "number":
            try:
                value = float(str(value).replace(",", "")) if str(value).strip() else None
            except ValueError:
                value = None
        elif op == "date":
            d = common.parse_dt(value)
            if d is None and isinstance(arg, str):
                try:
                    d = datetime.strptime(str(value), arg).replace(tzinfo=UTC)
                except ValueError:
                    d = None
            value = d.isoformat() if d else None
        elif op == "map":
            value = (arg or {}).get(str(value), value)
        elif op == "prefix":
            value = f"{arg}{value}"
        elif op == "suffix":
            value = f"{value}{arg}"
        elif op == "split":
            sep, i = (arg or [" ", 0])
            parts = str(value).split(sep)
            value = parts[i] if -len(parts) <= i < len(parts) else None
        else:
            raise HubError(f"Transformación desconocida: {op}", retryable=False)
    return value


def map_item(item: dict, fields: dict, country: str = "CO") -> dict:
    out = {}
    for local, spec in (fields or {}).items():
        if isinstance(spec, str):
            out[local] = jget(item, spec) if spec.startswith("$") else spec
        else:
            raw = jget(item, spec.get("path")) if spec.get("path") else spec.get("value")
            out[local] = transform(raw, spec.get("transforms") or [], item, country)
    return out


def render(value, ctx: dict):
    """Plantillas {{x}} en strings / dicts / listas (sin evaluar código)."""
    if isinstance(value, str):
        whole = _TPL.fullmatch(value.strip())
        if whole:
            return ctx.get(whole.group(1))
        return _TPL.sub(lambda m: "" if ctx.get(m.group(1)) is None else str(ctx[m.group(1)]), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def _private_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


# --- Validación de la definición ------------------------------------------------------------------------------
def validate(defn: dict) -> list[str]:
    errors = []
    base = str(defn.get("base_url") or "")
    if not base.startswith("https://"):
        errors.append("base_url debe empezar por https://")
    elif _private_host(base):
        errors.append("base_url no puede apuntar a una red interna")
    auth = defn.get("auth") or {}
    if auth.get("type", "none") not in AUTH_TYPES:
        errors.append(f"auth.type inválido ({', '.join(AUTH_TYPES)})")
    if auth.get("type") == "oauth2_client_credentials" and not str(auth.get("token_url") or "").startswith("https://"):
        errors.append("auth.token_url (https) es obligatorio para oauth2_client_credentials")
    keys = set()
    for i, ep in enumerate(defn.get("endpoints") or []):
        if not ep.get("key") or ep["key"] in keys:
            errors.append(f"endpoints[{i}]: key vacía o repetida")
        keys.add(ep.get("key"))
        if (ep.get("method") or "GET").upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            errors.append(f"endpoints[{i}]: método inválido")
        if not str(ep.get("path") or "").startswith("/"):
            errors.append(f"endpoints[{i}]: path debe empezar por /")
        if ((ep.get("pagination") or {}).get("type") or "none") not in PAGINATION:
            errors.append(f"endpoints[{i}]: paginación inválida ({', '.join(PAGINATION)})")
    for entity, m in (defn.get("mappings") or {}).items():
        if entity not in (*ENTITIES, "deal", "note", "find_contact"):
            errors.append(f"mappings.{entity}: entidad desconocida")
            continue
        for part in ("pull", "push") if entity != "find_contact" else ():
            spec = (m or {}).get(part)
            if spec and spec.get("endpoint") not in keys:
                errors.append(f"mappings.{entity}.{part}: endpoint «{spec.get('endpoint')}» no existe")
        if entity == "find_contact" and (m or {}).get("endpoint") not in keys:
            errors.append("mappings.find_contact: endpoint no existe")
        pull = (m or {}).get("pull") or {}
        required = {"contact": ("phone",), "product": ("sku", "name"), "order": ("external_id",)}.get(entity, ())
        if pull and entity != "order" and not any(r in (pull.get("fields") or {}) for r in required):
            errors.append(f"mappings.{entity}.pull.fields necesita {' o '.join(required)}")
        if pull and entity == "order" and not pull.get("id"):
            errors.append("mappings.order.pull.id (ruta del id del pedido) es obligatorio")
    return errors


# --- Cliente REST ------------------------------------------------------------------------------------------
class RestClient:
    def __init__(self, defn: ConnectorDefinition, secrets: dict[str, str | None]):
        self.defn, self.secrets = defn, secrets
        self.base = defn.base_url.rstrip("/")
        self._token: str | None = None

    async def _auth(self) -> tuple[dict, dict]:
        a = self.defn.auth or {}
        kind = a.get("type") or "none"
        headers, params = {"Accept": "application/json"}, {}
        if kind == "api_key":
            if a.get("query_param"):
                params[a["query_param"]] = self.secrets.get("api_key") or ""
            else:
                headers[a.get("header") or "X-API-Key"] = self.secrets.get("api_key") or ""
        elif kind == "bearer":
            headers["Authorization"] = f"Bearer {self.secrets.get('api_key') or ''}"
        elif kind == "basic":
            raw = f"{self.secrets.get('username') or ''}:{self.secrets.get('password') or self.secrets.get('api_key') or ''}"
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
        elif kind == "oauth2_client_credentials":
            if not self._token:
                r = http.check(await http.send("POST", a["token_url"], data={
                    "grant_type": "client_credentials", "client_id": self.secrets.get("client_id") or "",
                    "client_secret": self.secrets.get("client_secret") or "",
                    **({"scope": " ".join(a["scopes"])} if a.get("scopes") else {})}), self.defn.name)
                self._token = r.json().get("access_token")
            headers["Authorization"] = f"Bearer {self._token}"
        return headers, params

    def endpoint(self, key: str) -> dict:
        for ep in self.defn.endpoints or []:
            if ep.get("key") == key:
                return ep
        raise HubError(f"El conector no tiene el endpoint «{key}»", retryable=False)

    async def call(self, key: str, ctx: dict | None = None, body=None):
        ep = self.endpoint(key)
        ctx = ctx or {}
        headers, params = await self._auth()
        params = {**params, **{k: v for k, v in (render(ep.get("query") or {}, ctx)).items() if v not in (None, "")}}
        payload = body if body is not None else (render(ep["body"], ctx) if ep.get("body") is not None else None)
        r = http.check(await http.send((ep.get("method") or "GET").upper(), self.base + render(ep["path"], ctx),
                                       headers={**headers, "Content-Type": "application/json"}, json_body=payload,
                                       params=params), self.defn.name)
        return r.json() if r.content else None

    async def pages(self, key: str, ctx: dict | None = None, max_pages: int = MAX_PAGES):
        """Itera los ítems de un endpoint siguiendo su paginación."""
        ep = self.endpoint(key)
        pg = ep.get("pagination") or {}
        kind = pg.get("type") or "none"
        size = int(pg.get("size") or 100)
        ctx = dict(ctx or {})
        page, offset, cursor, url = int(pg.get("start", 1)), 0, None, None
        headers, base_params = await self._auth()
        for _ in range(max_pages):
            params = {**base_params, **{k: v for k, v in render(ep.get("query") or {}, ctx).items() if v not in (None, "")}}
            if kind == "page":
                params[pg.get("param") or "page"] = page
                if pg.get("size_param"):
                    params[pg["size_param"]] = size
            elif kind == "offset":
                params[pg.get("param") or "offset"] = offset
                params[pg.get("limit_param") or "limit"] = size
            elif kind == "cursor" and cursor:
                params[pg.get("param") or "cursor"] = cursor
            target = url or (self.base + render(ep["path"], ctx))
            r = http.check(await http.send((ep.get("method") or "GET").upper(), target, headers=headers,
                                           params=None if url else params), self.defn.name)
            data = r.json() if r.content else []
            items = jget(data, ep.get("items_path")) if ep.get("items_path") else data
            items = items if isinstance(items, list) else ([items] if items else [])
            for it in items:
                yield it
            if kind == "none" or not items:
                return
            if kind == "page":
                if len(items) < size:
                    return
                page += 1
            elif kind == "offset":
                if len(items) < size:
                    return
                offset += size
            elif kind == "cursor":
                cursor = jget(data, pg.get("next_path") or "$.next_cursor")
                if not cursor:
                    return
            elif kind == "link":
                url = jget(data, pg.get("next_path") or "$.links.next") or None
                if not url:
                    return
                if not str(url).startswith("http"):
                    url = self.base + str(url)
                if urlsplit(str(url)).hostname != urlsplit(self.base).hostname:
                    return  # no enviar credenciales a otro dominio


async def load(session: AsyncSession, conn: IntegrationConnection) -> tuple[ConnectorDefinition, RestClient]:
    defn = await session.get(ConnectorDefinition, conn.connector_id) if conn.connector_id else None
    if not defn or (defn.organization_id not in (None, conn.organization_id)):
        raise HubError("La conexión no tiene una definición de conector válida", retryable=False)
    secrets = {n: await common.secret(session, conn, n) for n in SECRET_NAMES}
    return defn, RestClient(defn, secrets)


# --- Probar endpoint ----------------------------------------------------------------------------------------
def _mask_obj(value, secrets: set[str]):
    if isinstance(value, str):
        for s in secrets:
            if s and len(s) >= 4 and s in value:
                value = value.replace(s, http.mask(s))
        return value
    if isinstance(value, dict):
        return {k: _mask_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_obj(v, secrets) for v in value]
    return value


async def test_endpoint(session: AsyncSession, conn: IntegrationConnection, key: str, entity: str | None = None) -> dict:
    """Trae SOLO la primera página (máx. 5 ítems) y muestra cómo quedarían mapeados. Secretos enmascarados."""
    defn, client = await load(session, conn)
    secrets = {v for v in client.secrets.values() if v}
    items = []
    try:
        async for it in client.pages(key, {"since": (utcnow() - timedelta(days=1)).isoformat()}, max_pages=1):
            items.append(it)
            if len(items) >= 5:
                break
    except HubError as e:
        return {"ok": False, "error": _mask_obj(str(e), secrets)}
    mapped = []
    if entity and (defn.mappings or {}).get(entity, {}).get("pull"):
        pull = defn.mappings[entity]["pull"]
        mapped = [map_item(it, pull.get("fields") or {}, (conn.settings or {}).get("country", "CO")) for it in items]
    return {"ok": True, "items": _mask_obj(items, secrets), "mapped": _mask_obj(mapped, secrets), "count": len(items)}


# --- Sincronización -----------------------------------------------------------------------------------------
async def _apply_contact(session: AsyncSession, conn: IntegrationConnection, data: dict) -> str:
    org = conn.organization_id
    phone, email = data.get("phone"), data.get("email")
    contact = await common.match_contact(session, org, email, phone, (conn.settings or {}).get("country", "CO"))
    created = False
    if contact is None:
        p = gn.phone(str(phone), (conn.settings or {}).get("country", "CO")) if phone else None
        if not p or not (conn.settings or {}).get("create_contacts", True):
            return "skipped"
        contact = Contact(organization_id=org, wa_id=p.value, name=data.get("name"))
        session.add(contact)
        created = True
    if data.get("name") and not contact.name:
        contact.name = str(data["name"])[:200]
    if email and not contact.email:
        e = gn.email(str(email))
        contact.email = e.value if e else contact.email
    await session.flush()
    return "created" if created else "updated"


async def sync_entity(session: AsyncSession, conn: IntegrationConnection, entity: str) -> dict:
    defn, client = await load(session, conn)
    pull = ((defn.mappings or {}).get(entity) or {}).get("pull")
    if not pull:
        return {"skipped": True}
    s = dict(conn.settings or {})
    cursors = dict(s.get("cursors") or {})
    since = common.parse_dt(cursors.get(entity))
    started = utcnow()
    country = s.get("country", "CO")
    run = await common.start_run(session, conn, entity, "pull", cursors.get(entity))
    error = None
    try:
        ctx = {"since": (since or (started - timedelta(days=int(s.get("initial_days", 30))))).isoformat()}
        async for item in client.pages(pull["endpoint"], ctx):
            run.fetched += 1
            data = map_item(item, pull.get("fields") or {}, country)
            ref = jget(item, pull.get("id")) if pull.get("id") else data.get("sku") or data.get("phone")
            try:
                async with session.begin_nested():
                    if entity == "product":
                        data.setdefault("external_id", ref)
                        res = await common.upsert_product(session, conn, data)
                    elif entity == "order":
                        order = {
                            "external_id": str(ref) if ref is not None else None,
                            "order_number": data.get("order_number"),
                            "status": data.get("status") if data.get("status") in common.ORDER_STATUSES else "pending",
                            "status_raw": data.get("status_raw") or data.get("status"),
                            "total": common.money(data.get("total")), "currency": data.get("currency"),
                            "items": data.get("items") if isinstance(data.get("items"), list) else [],
                            "customer": {"email": data.get("email"), "phone": data.get("phone"), "name": data.get("name")},
                            "placed_at": common.parse_dt(data.get("placed_at")), "raw": {"ref": ref}}
                        res = await common.upsert_order(session, conn, order)
                    else:
                        res = await _apply_contact(session, conn, data)
                setattr(run, res, getattr(run, res) + 1)
            except Exception as e:  # noqa: BLE001
                common.note_error(run, ref, e)
        cursors[entity] = (started - timedelta(minutes=2)).isoformat()
    except HubError as e:
        error = str(e)
        if not e.retryable:
            conn.status, conn.last_error = "error", error[:500]
    common.finish_run(run, error, cursors.get(entity))
    s["cursors"] = cursors
    conn.settings = s
    conn.last_sync_at = utcnow()
    await session.commit()
    return {"fetched": run.fetched, "created": run.created, "updated": run.updated, "failed": run.failed, "error": error}


# --- Webhooks ------------------------------------------------------------------------------------------------
def verify_webhook(defn: ConnectorDefinition, secret: str | None, headers: dict, body: bytes) -> bool:
    w = defn.webhooks or {}
    header = headers.get((w.get("secret_header") or "X-Signature").lower())
    if not secret or not header:
        return False
    if (w.get("signature") or "hmac_sha256") == "token":
        return hmac.compare_digest(secret, header)
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode() if w.get("encoding") == "base64" else digest.hex()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def apply_webhook(session: AsyncSession, conn: IntegrationConnection, defn: ConnectorDefinition, payload) -> dict:
    w = defn.webhooks or {}
    entity = w.get("entity") or "order"
    pull = ((defn.mappings or {}).get(entity) or {}).get("pull") or {}
    items = jget(payload, w["items_path"]) if w.get("items_path") else payload
    items = items if isinstance(items, list) else [items]
    run = await common.start_run(session, conn, entity, "webhook")
    country = (conn.settings or {}).get("country", "CO")
    for item in items:
        run.fetched += 1
        data = map_item(item, pull.get("fields") or {}, country)
        ref = jget(item, pull.get("id")) if pull.get("id") else data.get("sku")
        try:
            async with session.begin_nested():
                if entity == "order":
                    res = await common.upsert_order(session, conn, {
                        "external_id": str(ref) if ref is not None else None, "order_number": data.get("order_number"),
                        "status": data.get("status") if data.get("status") in common.ORDER_STATUSES else "pending",
                        "status_raw": data.get("status"), "total": common.money(data.get("total")),
                        "currency": data.get("currency"), "items": data.get("items") or [],
                        "customer": {"email": data.get("email"), "phone": data.get("phone"), "name": data.get("name")},
                        "placed_at": common.parse_dt(data.get("placed_at")), "raw": {"webhook": True}})
                elif entity == "product":
                    data.setdefault("external_id", ref)
                    res = await common.upsert_product(session, conn, data)
                else:
                    res = await _apply_contact(session, conn, data)
            setattr(run, res, getattr(run, res) + 1)
        except Exception as e:  # noqa: BLE001
            common.note_error(run, ref, e)
    common.finish_run(run)
    await session.commit()
    return {"received": run.fetched, "created": run.created, "updated": run.updated, "failed": run.failed}


# --- Adaptador CRM (push por el outbox) ---------------------------------------------------------------------
class CustomRestAdapter(CRMAdapter):
    """Envía contactos/negocios al sistema propio con los endpoints `push` del conector. Las propiedades vienen de
    integration_mappings (local → propiedad remota)."""

    provider = "custom"

    def __init__(self, defn: ConnectorDefinition, client: RestClient):
        self.defn, self.client = defn, client

    def _push(self, entity: str) -> dict:
        spec = ((self.defn.mappings or {}).get(entity) or {}).get("push")
        if not spec:
            raise CRMError(f"El conector no envía {entity}s (sin endpoint push)", retryable=False, status=400)
        return spec

    async def _send(self, spec: dict, ctx: dict, body: dict) -> str | None:
        try:
            data = await self.client.call(spec["endpoint"], ctx, body)
        except HubError as e:
            raise CRMError(str(e), retryable=e.retryable, status=e.status) from e
        rid = jget(data, spec.get("id_path") or "$.id") if data is not None else None
        return str(rid) if rid not in (None, "") else ctx.get("id")

    async def find_contact(self, email, phone):
        spec = (self.defn.mappings or {}).get("find_contact")
        if not spec:
            return None
        try:
            data = await self.client.call(spec["endpoint"], {"email": email or "", "phone": phone or ""})
        except HubError as e:
            if e.status == 404:
                return None
            raise CRMError(str(e), retryable=e.retryable, status=e.status) from e
        rid = jget(data, spec.get("id_path") or "$.id")
        return str(rid) if rid not in (None, "", []) else None

    async def upsert_contact(self, remote_id, props):
        return await self._send(self._push("contact"), {"id": remote_id}, props) or remote_id or ""

    async def upsert_deal(self, remote_id, props, contact_remote_id):
        body = {**props, **({"contact_id": contact_remote_id} if contact_remote_id else {})}
        return await self._send(self._push("deal"), {"id": remote_id}, body) or remote_id or ""

    async def add_note(self, contact_remote_id, text, deal_remote_id=None):
        spec = ((self.defn.mappings or {}).get("note") or {}).get("push")
        if not spec:
            return ""  # el sistema no recibe notas: no es un error
        return await self._send(spec, {"id": contact_remote_id}, {"contact_id": contact_remote_id, "text": text}) or ""

    async def changed_contacts(self, since, properties):
        return []  # la lectura la hace el hub (sync_entity con el mapeo JSONPath), no el ciclo CRM

    async def changed_deals(self, since, properties):
        return []


def template_definitions() -> list[dict]:
    """Plantillas de la plataforma (organization_id null) para empezar."""
    return [
        {"key": "dms_rest", "name": "DMS REST (concesionario)",
         "description": "Clientes, vehículos/inventario y pedidos de un DMS con API REST paginada por página.",
         "base_url": "https://api.tu-dms.com/v1", "auth": {"type": "api_key", "header": "X-API-Key"},
         "endpoints": [
             {"key": "list_customers", "method": "GET", "path": "/customers", "query": {"updated_since": "{{since}}"},
              "pagination": {"type": "page", "param": "page", "size_param": "per_page", "size": 100}, "items_path": "$.data"},
             {"key": "list_vehicles", "method": "GET", "path": "/inventory", "query": {"updated_since": "{{since}}"},
              "pagination": {"type": "page", "param": "page", "size_param": "per_page", "size": 100}, "items_path": "$.data"},
             {"key": "list_orders", "method": "GET", "path": "/sales", "query": {"updated_since": "{{since}}"},
              "pagination": {"type": "cursor", "param": "cursor", "next_path": "$.next_cursor"}, "items_path": "$.data"},
             {"key": "upsert_customer", "method": "POST", "path": "/customers"},
             {"key": "find_customer", "method": "GET", "path": "/customers/search", "query": {"phone": "{{phone}}"}}],
         "mappings": {
             "contact": {"pull": {"endpoint": "list_customers", "id": "$.id", "updated": "$.updated_at",
                                  "fields": {"name": {"path": "$.full_name", "transforms": ["trim"]},
                                             "email": {"path": "$.email", "transforms": ["trim", "lower"]},
                                             "phone": {"path": "$.mobile", "transforms": ["phone_e164"]}}},
                         "push": {"endpoint": "upsert_customer", "id_path": "$.id"}},
             "product": {"pull": {"endpoint": "list_vehicles", "id": "$.stock_id",
                                  "fields": {"sku": "$.vin", "name": {"value": None, "transforms": [
                                      {"concat": ["$.make", "$.model", "$.version", "$.year"]}]},
                                             "price": {"path": "$.price", "transforms": ["number"]},
                                             "brand": "$.make", "category": "$.condition", "image_url": "$.photo_url"}}},
             "order": {"pull": {"endpoint": "list_orders", "id": "$.sale_id",
                                "fields": {"order_number": "$.invoice_number", "total": "$.total", "currency": "$.currency",
                                           "status": {"path": "$.status", "transforms": [{"map": {
                                               "invoiced": "paid", "delivered": "fulfilled", "void": "cancelled"}}]},
                                           "email": "$.customer.email",
                                           "phone": {"path": "$.customer.mobile", "transforms": ["phone_e164"]},
                                           "placed_at": "$.date"}}},
             "find_contact": {"endpoint": "find_customer", "id_path": "$.data[0].id"}},
         "webhooks": {"secret_header": "X-DMS-Signature", "signature": "hmac_sha256", "encoding": "hex", "entity": "order"}},
        {"key": "erp_inventario", "name": "ERP de inventario y repuestos",
         "description": "Catálogo de repuestos/accesorios con existencias desde un ERP con OAuth2 (client credentials).",
         "base_url": "https://erp.tu-empresa.com/api",
         "auth": {"type": "oauth2_client_credentials", "token_url": "https://erp.tu-empresa.com/oauth/token"},
         "endpoints": [{"key": "list_items", "method": "GET", "path": "/items", "query": {"modified_after": "{{since}}"},
                        "pagination": {"type": "offset", "param": "offset", "limit_param": "limit", "size": 200},
                        "items_path": "$.items"}],
         "mappings": {"product": {"pull": {"endpoint": "list_items", "id": "$.code",
                                           "fields": {"sku": "$.code", "name": {"path": "$.description", "transforms": ["trim"]},
                                                      "price": {"path": "$.list_price", "transforms": ["number"]},
                                                      "stock": "$.on_hand", "category": "$.family"}}}},
         "webhooks": {}},
    ]


def definition_json(d: ConnectorDefinition) -> dict:
    return {"id": d.id, "key": d.key, "name": d.name, "description": d.description, "base_url": d.base_url,
            "auth": d.auth or {}, "endpoints": d.endpoints or [], "mappings": d.mappings or {},
            "webhooks": d.webhooks or {}, "version": d.version, "is_published": d.is_published,
            "template": d.organization_id is None, "updated_at": d.updated_at}


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)
