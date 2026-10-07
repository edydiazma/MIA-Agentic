"""Adaptador Salesforce (REST API: Lead/Contact, Opportunity, Task)."""

from datetime import UTC, date, datetime

from app.crm.base import CRMAdapter, RemoteRecord, check, send

API_VERSION = "v61.0"
SCOPES = ["api", "refresh_token", "offline_access"]


def soql_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:  # 2026-10-08T12:00:00.000+0000
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


class SalesforceAdapter(CRMAdapter):
    provider = "salesforce"

    def __init__(self, token: str, instance_url: str, contact_object: str = "Contact"):
        self.base = f"{instance_url.rstrip('/')}/services/data/{API_VERSION}"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.contact_object = contact_object if contact_object in ("Contact", "Lead") else "Contact"

    async def _call(self, method: str, path: str, body=None, params=None):
        return check(await send(method, f"{self.base}{path}", headers=self.headers, json_body=body, params=params))

    async def query(self, soql: str) -> list[dict]:
        records, data = [], (await self._call("GET", "/query", params={"q": soql})).json()
        records += data.get("records") or []
        while data.get("nextRecordsUrl") and len(records) < 2000:
            url = data["nextRecordsUrl"].split(f"/services/data/{API_VERSION}", 1)[-1]
            data = (await self._call("GET", url)).json()
            records += data.get("records") or []
        return records

    async def find_contact(self, email, phone):
        conds = []
        if email:
            conds.append(f"Email = {soql_str(email)}")
        if phone:
            conds += [f"Phone = {soql_str(phone)}", f"MobilePhone = {soql_str(phone)}"]
        if not conds:
            return None
        rows = await self.query(f"SELECT Id FROM {self.contact_object} WHERE {' OR '.join(conds)} LIMIT 1")
        return rows[0]["Id"] if rows else None

    async def upsert_contact(self, remote_id, props):
        if self.contact_object == "Lead":
            props = {"Company": props.get("Company") or "WhatsApp", **props}
        if not props.get("LastName"):
            props = {**props, "LastName": props.get("FirstName") or "Cliente WhatsApp"}
        if remote_id:
            await self._call("PATCH", f"/sobjects/{self.contact_object}/{remote_id}", props)
            return str(remote_id)
        return str((await self._call("POST", f"/sobjects/{self.contact_object}", props)).json()["id"])

    async def upsert_deal(self, remote_id, props, contact_remote_id):
        props = dict(props)
        props.setdefault("CloseDate", date.today().isoformat())  # obligatorio en Salesforce
        props.setdefault("StageName", "Prospecting")
        if remote_id:
            await self._call("PATCH", f"/sobjects/Opportunity/{remote_id}", props)
            return str(remote_id)
        opp_id = str((await self._call("POST", "/sobjects/Opportunity", props)).json()["id"])
        if contact_remote_id and self.contact_object == "Contact":
            await self._call("POST", "/sobjects/OpportunityContactRole",
                             {"OpportunityId": opp_id, "ContactId": contact_remote_id, "IsPrimary": True})
        return opp_id

    async def add_note(self, contact_remote_id, text, deal_remote_id=None):
        body = {"Subject": "Conversación de WhatsApp", "Description": text[:32000], "WhoId": contact_remote_id,
                "Status": "Completed", "ActivityDate": datetime.now(UTC).date().isoformat()}
        if deal_remote_id and self.contact_object == "Contact":
            body["WhatId"] = deal_remote_id
        return str((await self._call("POST", "/sobjects/Task", body)).json()["id"])

    async def _changed(self, obj: str, since: datetime, properties: list[str]) -> list[RemoteRecord]:
        fields = sorted({"Id", "LastModifiedDate", *properties})
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = await self.query(f"SELECT {', '.join(fields)} FROM {obj} WHERE LastModifiedDate > {stamp} "
                                "ORDER BY LastModifiedDate ASC LIMIT 2000")
        return [RemoteRecord(id=r["Id"], properties={k: v for k, v in r.items() if k != "attributes"},
                             updated_at=_parse(r.get("LastModifiedDate"))) for r in rows]

    async def changed_contacts(self, since, properties):
        return await self._changed(self.contact_object, since, properties)

    async def changed_deals(self, since, properties):
        return await self._changed("Opportunity", since, properties)


def authorize_url(login_url: str) -> str:
    return f"{login_url.rstrip('/')}/services/oauth2/authorize"


async def exchange_code(login_url: str, client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    r = check(await send("POST", f"{login_url.rstrip('/')}/services/oauth2/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "code": code}))
    return r.json()


async def refresh(login_url: str, client_id: str, client_secret: str, refresh_token: str) -> dict:
    r = check(await send("POST", f"{login_url.rstrip('/')}/services/oauth2/token", data={
        "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token}))
    return r.json()


async def revoke(login_url: str, token: str) -> None:
    await send("POST", f"{login_url.rstrip('/')}/services/oauth2/revoke", data={"token": token})
