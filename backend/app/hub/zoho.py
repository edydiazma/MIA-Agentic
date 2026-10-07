"""Adaptador Zoho CRM (API v8) del marco de CRM (app/crm): contactos, negocios (Deals), notas y cambios.

Autenticación OAuth: el `api_domain` que devuelve Zoho al emitir el token (p. ej. https://www.zohoapis.com o
.eu/.in) queda en integration_connections.instance_url.
"""

from datetime import UTC, datetime
from email.utils import format_datetime

from app.config import get_settings
from app.crm.base import CRMAdapter, CRMError, RemoteRecord, check, send

SCOPES = ["ZohoCRM.modules.contacts.ALL", "ZohoCRM.modules.deals.ALL", "ZohoCRM.modules.notes.ALL",
          "ZohoCRM.users.READ", "ZohoCRM.org.READ"]
API_VERSION = "v8"


def accounts_url() -> str:
    return (get_settings().zoho_accounts_url or "https://accounts.zoho.com").rstrip("/")


def authorize_url() -> str:
    return f"{accounts_url()}/oauth/v2/auth"


async def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    return check(await send("POST", f"{accounts_url()}/oauth/v2/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "code": code})).json()


async def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    data = check(await send("POST", f"{accounts_url()}/oauth/v2/token", data={
        "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token})).json()
    if data.get("error"):
        raise CRMError(f"Zoho: {data['error']}", retryable=False, status=401)
    return data


async def revoke(token: str) -> None:
    await send("POST", f"{accounts_url()}/oauth/v2/token/revoke", params={"token": token})


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class ZohoAdapter(CRMAdapter):
    provider = "zoho"

    def __init__(self, token: str, api_domain: str):
        self.base = f"{(api_domain or 'https://www.zohoapis.com').rstrip('/')}/crm/{API_VERSION}"
        self.headers = {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, body=None, params=None, headers=None):
        return check(await send(method, f"{self.base}{path}", headers={**self.headers, **(headers or {})},
                                json_body=body, params=params))

    @staticmethod
    def _id(r) -> str:
        row = (r.json().get("data") or [{}])[0]
        if row.get("status") == "error":
            raise CRMError(f"Zoho: {row.get('message')} ({row.get('code')})", retryable=False, status=400)
        return str((row.get("details") or {}).get("id") or row.get("id"))

    async def org(self) -> dict:
        return ((await self._call("GET", "/org")).json().get("org") or [{}])[0]

    async def find_contact(self, email, phone):
        for key, value in (("email", email), ("phone", phone)):
            if not value:
                continue
            r = await self._call("GET", "/Contacts/search", params={key: value})
            if r.status_code == 204 or not r.content:
                continue
            rows = r.json().get("data") or []
            if rows:
                return str(rows[0]["id"])
        return None

    async def upsert_contact(self, remote_id, props):
        if remote_id:
            return self._id(await self._call("PUT", "/Contacts", {"data": [{"id": remote_id, **props}]}))
        props = {"Last_Name": props.get("Last_Name") or props.get("First_Name") or "Cliente WhatsApp", **props}
        return self._id(await self._call("POST", "/Contacts", {"data": [props]}))

    async def upsert_deal(self, remote_id, props, contact_remote_id):
        data = dict(props)
        if contact_remote_id:
            data["Contact_Name"] = {"id": contact_remote_id}
        if remote_id:
            return self._id(await self._call("PUT", "/Deals", {"data": [{"id": remote_id, **data}]}))
        data.setdefault("Deal_Name", "Negocio WhatsApp")
        data.setdefault("Stage", "Qualification")
        return self._id(await self._call("POST", "/Deals", {"data": [data]}))

    async def add_note(self, contact_remote_id, text, deal_remote_id=None):
        module, rid = ("Deals", deal_remote_id) if deal_remote_id else ("Contacts", contact_remote_id)
        return self._id(await self._call("POST", f"/{module}/{rid}/Notes",
                                         {"data": [{"Note_Title": "Conversación de WhatsApp", "Note_Content": text}]}))

    async def _changed(self, module: str, since: datetime, properties: list[str]) -> list[RemoteRecord]:
        fields = ",".join(sorted({*properties, "Modified_Time"}))[:2000]
        out = []
        for page in range(1, 11):  # máx. 2000 registros por ciclo
            r = await self._call("GET", f"/{module}", params={"fields": fields, "sort_by": "Modified_Time",
                                                              "sort_order": "asc", "page": page, "per_page": 200},
                                 headers={"If-Modified-Since": format_datetime(since.astimezone(UTC), usegmt=True)})
            if r.status_code in (204, 304) or not r.content:
                break
            data = r.json()
            for row in data.get("data") or []:
                out.append(RemoteRecord(id=str(row["id"]), properties={k: v for k, v in row.items() if k != "id"},
                                        updated_at=_parse(row.get("Modified_Time"))))
            if not (data.get("info") or {}).get("more_records"):
                break
        return out

    async def changed_contacts(self, since, properties):
        return await self._changed("Contacts", since, properties)

    async def changed_deals(self, since, properties):
        return await self._changed("Deals", since, properties)


DEFAULT_MAPPINGS = [
    {"object": "contact", "local_field": "first_name", "remote_property": "First_Name", "direction": "both"},
    {"object": "contact", "local_field": "last_name", "remote_property": "Last_Name", "direction": "both"},
    {"object": "contact", "local_field": "email", "remote_property": "Email", "direction": "both"},
    {"object": "contact", "local_field": "phone", "remote_property": "Mobile", "direction": "push"},
    {"object": "deal", "local_field": "deal.name", "remote_property": "Deal_Name", "direction": "both"},
    {"object": "deal", "local_field": "deal.amount", "remote_property": "Amount", "direction": "both"},
    {"object": "deal", "local_field": "deal.stage", "remote_property": "Stage", "direction": "push",
     "transform": {"map": {"new": "Qualification", "qualified": "Needs Analysis", "proposal": "Proposal/Price Quote",
                           "negotiation": "Negotiation/Review", "won": "Closed Won", "lost": "Closed Lost"}}},
    {"object": "deal", "local_field": "deal.close_date", "remote_property": "Closing_Date", "direction": "push"},
]
