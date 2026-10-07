"""Adaptador Odoo (JSON-RPC externo con clave de API) del marco de CRM (app/crm).

res.partner ↔ contactos, crm.lead (tipo oportunidad) ↔ negocios, notas con message_post. Credenciales: URL, base
de datos, usuario (correo) y clave de API (Vault). Pedidos de venta (sale.order) se leen desde el hub.
"""

from datetime import UTC, datetime

from app.crm.base import CRMAdapter, CRMError, RemoteRecord, TokenRevoked, check, send

ODOO_DT = "%Y-%m-%d %H:%M:%S"


def _parse(v) -> datetime | None:
    if not v or v is False:
        return None
    try:
        return datetime.strptime(str(v)[:19], ODOO_DT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _clean(props: dict) -> dict:
    """Odoo usa False en lugar de null."""
    return {k: (False if v is None or v == "" else v) for k, v in props.items()}


class OdooAdapter(CRMAdapter):
    provider = "odoo"

    def __init__(self, url: str, db: str, login: str, api_key: str):
        if not url.startswith("https://"):
            raise CRMError("La URL de Odoo debe usar https", retryable=False)
        self.url, self.db, self.login, self.key = url.rstrip("/"), db, login, api_key
        self.uid: int | None = None

    async def _rpc(self, service: str, method: str, *args):
        r = check(await send("POST", f"{self.url}/jsonrpc", headers={"Content-Type": "application/json"}, json_body={
            "jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": list(args)},
            "id": 1}))
        data = r.json()
        if data.get("error"):
            err = data["error"]
            msg = ((err.get("data") or {}).get("message") or err.get("message") or "error")
            name = (err.get("data") or {}).get("name") or ""
            if "AccessDenied" in name or "Access Denied" in msg:
                raise TokenRevoked("Odoo rechazó el usuario o la clave de API")
            raise CRMError(f"Odoo: {msg[:400]}", retryable=False, status=400)
        return data.get("result")

    async def authenticate(self) -> int:
        if self.uid is None:
            uid = await self._rpc("common", "authenticate", self.db, self.login, self.key, {})
            if not uid:
                raise TokenRevoked("Odoo rechazó el usuario o la clave de API")
            self.uid = int(uid)
        return self.uid

    async def execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        uid = await self.authenticate()
        return await self._rpc("object", "execute_kw", self.db, uid, self.key, model, method, args, kwargs or {})

    async def version(self) -> dict:
        return await self._rpc("common", "version")

    async def find_contact(self, email, phone):
        domain = []
        if email:
            domain.append(("email", "=ilike", email))
        if phone:
            domain += [("phone", "=", phone), ("mobile", "=", phone)]
        if not domain:
            return None
        domain = ["|"] * (len(domain) - 1) + domain
        rows = await self.execute("res.partner", "search", [domain], {"limit": 1})
        return str(rows[0]) if rows else None

    async def upsert_contact(self, remote_id, props):
        props = _clean(props)
        if remote_id:
            await self.execute("res.partner", "write", [[int(remote_id)], props])
            return str(remote_id)
        props.setdefault("name", "Cliente WhatsApp")
        return str(await self.execute("res.partner", "create", [props]))

    async def upsert_deal(self, remote_id, props, contact_remote_id):
        data = _clean(props)
        if contact_remote_id:
            data["partner_id"] = int(contact_remote_id)
        if remote_id:
            await self.execute("crm.lead", "write", [[int(remote_id)], data])
            return str(remote_id)
        data.setdefault("name", "Oportunidad WhatsApp")
        data["type"] = "opportunity"
        return str(await self.execute("crm.lead", "create", [data]))

    async def add_note(self, contact_remote_id, text, deal_remote_id=None):
        model, rid = ("crm.lead", deal_remote_id) if deal_remote_id else ("res.partner", contact_remote_id)
        body = text.replace("\n", "<br/>")
        return str(await self.execute(model, "message_post", [[int(rid)]], {"body": body}))

    async def _changed(self, model: str, since: datetime, properties: list[str], extra: list | None = None):
        fields = sorted({*properties, "write_date"})
        domain = [("write_date", ">", since.astimezone(UTC).strftime(ODOO_DT)), *(extra or [])]
        out, offset = [], 0
        for _ in range(10):
            rows = await self.execute(model, "search_read", [domain],
                                      {"fields": fields, "order": "write_date asc", "limit": 200, "offset": offset})
            for row in rows or []:
                props = {k: (v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) and k != "tag_ids" else v)
                         for k, v in row.items() if k != "id"}
                out.append(RemoteRecord(id=str(row["id"]), properties=props, updated_at=_parse(row.get("write_date"))))
            if not rows or len(rows) < 200:
                break
            offset += 200
        return out

    async def changed_contacts(self, since, properties):
        return await self._changed("res.partner", since, properties)

    async def changed_deals(self, since, properties):
        return await self._changed("crm.lead", since, properties, [("type", "=", "opportunity")])

    async def sale_orders(self, since: datetime | None, limit: int = 200) -> list[dict]:
        domain = [("write_date", ">", since.astimezone(UTC).strftime(ODOO_DT))] if since else []
        return await self.execute("sale.order", "search_read", [domain], {
            "fields": ["name", "partner_id", "amount_total", "currency_id", "state", "date_order", "order_line"],
            "order": "write_date asc", "limit": limit}) or []


DEFAULT_MAPPINGS = [
    {"object": "contact", "local_field": "name", "remote_property": "name", "direction": "both"},
    {"object": "contact", "local_field": "email", "remote_property": "email", "direction": "both"},
    {"object": "contact", "local_field": "phone", "remote_property": "mobile", "direction": "push"},
    {"object": "deal", "local_field": "deal.name", "remote_property": "name", "direction": "both"},
    {"object": "deal", "local_field": "deal.amount", "remote_property": "expected_revenue", "direction": "both"},
    {"object": "deal", "local_field": "deal.close_date", "remote_property": "date_deadline", "direction": "push"},
]
