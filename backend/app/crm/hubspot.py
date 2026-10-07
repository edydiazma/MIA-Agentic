"""Adaptador HubSpot (CRM API v3/v4)."""

from datetime import UTC, datetime

from app.crm.base import CRMAdapter, RemoteRecord, check, send

API = "https://api.hubapi.com"
AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
TOKEN_URL = f"{API}/oauth/v1/token"
SCOPES = ["oauth", "crm.objects.contacts.read", "crm.objects.contacts.write", "crm.objects.deals.read",
          "crm.objects.deals.write"]
NOTE_TO_CONTACT = 202  # associationTypeId HUBSPOT_DEFINED note → contact
NOTE_TO_DEAL = 214


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class HubSpotAdapter(CRMAdapter):
    provider = "hubspot"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, body=None, params=None):
        return check(await send(method, f"{API}{path}", headers=self.headers, json_body=body, params=params))

    async def account(self) -> dict:
        return (await self._call("GET", "/account-info/v3/details")).json()

    async def find_contact(self, email, phone):
        groups = []
        if email:
            groups.append({"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]})
        if phone:
            for prop in ("phone", "mobilephone"):
                groups.append({"filters": [{"propertyName": prop, "operator": "EQ", "value": phone}]})
        if not groups:
            return None
        r = await self._call("POST", "/crm/v3/objects/contacts/search",
                             {"filterGroups": groups[:5], "properties": ["email"], "limit": 1})
        results = r.json().get("results") or []
        return str(results[0]["id"]) if results else None

    async def upsert_contact(self, remote_id, props):
        if remote_id:
            await self._call("PATCH", f"/crm/v3/objects/contacts/{remote_id}", {"properties": props})
            return str(remote_id)
        r = await self._call("POST", "/crm/v3/objects/contacts", {"properties": props})
        return str(r.json()["id"])

    async def upsert_deal(self, remote_id, props, contact_remote_id):
        if remote_id:
            await self._call("PATCH", f"/crm/v3/objects/deals/{remote_id}", {"properties": props})
            deal_id = str(remote_id)
        else:
            deal_id = str((await self._call("POST", "/crm/v3/objects/deals", {"properties": props})).json()["id"])
        if contact_remote_id:
            await self._call("PUT", f"/crm/v4/objects/deals/{deal_id}/associations/default/contacts/{contact_remote_id}")
        return deal_id

    async def add_note(self, contact_remote_id, text, deal_remote_id=None):
        assoc = [{"to": {"id": contact_remote_id},
                  "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}]}]
        if deal_remote_id:
            assoc.append({"to": {"id": deal_remote_id},
                          "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_DEAL}]})
        r = await self._call("POST", "/crm/v3/objects/notes", {
            "properties": {"hs_note_body": text.replace("\n", "<br>"), "hs_timestamp": _ms(datetime.now(UTC))},
            "associations": assoc})
        return str(r.json()["id"])

    async def _changed(self, obj: str, since: datetime, properties: list[str]) -> list[RemoteRecord]:
        prop = "lastmodifieddate" if obj == "contacts" else "hs_lastmodifieddate"
        out, after = [], None
        for _ in range(20):  # máx. 2000 registros por ciclo
            body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "GT", "value": _ms(since)}]}],
                    "sorts": [{"propertyName": prop, "direction": "ASCENDING"}],
                    "properties": list({*properties, prop}), "limit": 100}
            if after:
                body["after"] = after
            data = (await self._call("POST", f"/crm/v3/objects/{obj}/search", body)).json()
            for row in data.get("results") or []:
                p = row.get("properties") or {}
                out.append(RemoteRecord(id=str(row["id"]), properties=p,
                                        updated_at=_parse(p.get(prop) or row.get("updatedAt"))))
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out

    async def changed_contacts(self, since, properties):
        return await self._changed("contacts", since, properties)

    async def changed_deals(self, since, properties):
        return await self._changed("deals", since, properties)


async def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    r = check(await send("POST", TOKEN_URL, data={
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "code": code}))
    return r.json()


async def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    r = check(await send("POST", TOKEN_URL, data={
        "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token}))
    return r.json()


async def revoke(refresh_token: str) -> None:
    await send("DELETE", f"{API}/oauth/v1/refresh_tokens/{refresh_token}")
