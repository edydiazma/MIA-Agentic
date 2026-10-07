"""Conexiones OAuth con HubSpot/Salesforce: estado firmado, tokens en Vault, refresco y adaptadores."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crm import hubspot, salesforce
from app.crm.base import CRMAdapter, CRMError
from app.models import ExternalLink, IntegrationConnection, IntegrationMapping, utcnow
from app.secrets_vault import delete_secret, get_secret, put_secret

STATE_AUDIENCE = "crm-oauth"


def configured(provider: str) -> bool:
    s = get_settings()
    if provider == "hubspot":
        return bool(s.hubspot_client_id and s.hubspot_client_secret)
    if provider == "salesforce":
        return bool(s.salesforce_client_id and s.salesforce_client_secret)
    if provider == "zoho":
        return bool(s.zoho_client_id and s.zoho_client_secret)
    return provider == "odoo"  # Odoo: credenciales por organización (URL, base, usuario, clave de API)


def redirect_uri(provider: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/integrations/{provider}/callback"


def make_state(org: int, agent_id: int, provider: str) -> str:
    s = get_settings()
    return jwt.encode({"org": org, "agent": agent_id, "provider": provider, "aud": STATE_AUDIENCE,
                       "exp": datetime.now(UTC) + timedelta(minutes=10)}, s.jwt_secret, algorithm="HS256")


def read_state(state: str, provider: str) -> dict:
    data = jwt.decode(state, get_settings().jwt_secret, algorithms=["HS256"], audience=STATE_AUDIENCE)
    if data.get("provider") != provider:
        raise jwt.InvalidTokenError("proveedor distinto")
    return data


def authorize_url(provider: str, org: int, agent_id: int) -> str:
    s = get_settings()
    state = make_state(org, agent_id, provider)
    if provider == "hubspot":
        return hubspot.AUTHORIZE_URL + "?" + urlencode({
            "client_id": s.hubspot_client_id, "redirect_uri": redirect_uri(provider),
            "scope": " ".join(hubspot.SCOPES), "state": state})
    if provider == "zoho":
        from app.hub import zoho

        return zoho.authorize_url() + "?" + urlencode({
            "response_type": "code", "client_id": s.zoho_client_id, "redirect_uri": redirect_uri(provider),
            "scope": ",".join(zoho.SCOPES), "access_type": "offline", "prompt": "consent", "state": state})
    return salesforce.authorize_url(s.salesforce_login_url) + "?" + urlencode({
        "response_type": "code", "client_id": s.salesforce_client_id, "redirect_uri": redirect_uri(provider),
        "scope": " ".join(salesforce.SCOPES), "state": state})


def default_mappings(provider: str, contact_object: str = "Contact") -> list[dict]:
    """Mapeo inicial (editable). local_field: name/first_name/last_name/email/phone/stage/notes/memory/custom:<k>."""
    if provider == "hubspot":
        return [
            {"object": "contact", "local_field": "first_name", "remote_property": "firstname", "direction": "both"},
            {"object": "contact", "local_field": "last_name", "remote_property": "lastname", "direction": "both"},
            {"object": "contact", "local_field": "email", "remote_property": "email", "direction": "both"},
            {"object": "contact", "local_field": "phone", "remote_property": "phone", "direction": "push"},
            {"object": "contact", "local_field": "stage", "remote_property": "lifecyclestage", "direction": "push",
             "transform": {"map": {"lead": "lead", "prospect": "marketingqualifiedlead", "client": "customer",
                                   "lost": "other"}}},
            {"object": "deal", "local_field": "deal.name", "remote_property": "dealname", "direction": "both"},
            {"object": "deal", "local_field": "deal.amount", "remote_property": "amount", "direction": "both"},
            {"object": "deal", "local_field": "deal.stage", "remote_property": "dealstage", "direction": "push",
             "transform": {"map": {"new": "appointmentscheduled", "qualified": "qualifiedtobuy",
                                   "proposal": "presentationscheduled", "negotiation": "decisionmakerboughtin",
                                   "won": "closedwon", "lost": "closedlost"}}},
            {"object": "deal", "local_field": "deal.close_date", "remote_property": "closedate", "direction": "push"},
        ]
    if provider in ("zoho", "odoo"):
        from app.hub import odoo, zoho

        return [dict(m) for m in (zoho if provider == "zoho" else odoo).DEFAULT_MAPPINGS]
    if provider == "custom":
        return [{"object": "contact", "local_field": f, "remote_property": f, "direction": "push"}
                for f in ("name", "email", "phone")]
    return [
        {"object": "contact", "local_field": "first_name", "remote_property": "FirstName", "direction": "both"},
        {"object": "contact", "local_field": "last_name", "remote_property": "LastName", "direction": "both"},
        {"object": "contact", "local_field": "email", "remote_property": "Email", "direction": "both"},
        {"object": "contact", "local_field": "phone", "remote_property": "MobilePhone", "direction": "push"},
        {"object": "deal", "local_field": "deal.name", "remote_property": "Name", "direction": "both"},
        {"object": "deal", "local_field": "deal.amount", "remote_property": "Amount", "direction": "both"},
        {"object": "deal", "local_field": "deal.stage", "remote_property": "StageName", "direction": "push",
         "transform": {"map": {"new": "Prospecting", "qualified": "Qualification", "proposal": "Proposal/Price Quote",
                               "negotiation": "Negotiation/Review", "won": "Closed Won", "lost": "Closed Lost"}}},
        {"object": "deal", "local_field": "deal.close_date", "remote_property": "CloseDate", "direction": "push"},
    ]


async def get_connection(session: AsyncSession, org: int, provider: str) -> IntegrationConnection | None:
    return await session.scalar(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider == provider))


async def upsert_connection(session: AsyncSession, org: int, provider: str, agent_id: int | None,
                            tokens: dict, external_account_id: str | None, instance_url: str | None = None,
                            scopes: list[str] | None = None) -> IntegrationConnection:
    conn = await get_connection(session, org, provider)
    new = conn is None
    if new:
        conn = IntegrationConnection(organization_id=org, provider=provider, connected_by=agent_id,
                                     settings={"contact_object": "Contact", "push_contacts": "all", "push_notes": True})
        session.add(conn)
        await session.flush()
    conn.status, conn.last_error = "connected", None
    conn.external_account_id = external_account_id or conn.external_account_id
    conn.instance_url = instance_url or conn.instance_url
    conn.scopes = scopes or conn.scopes or []
    conn.connected_by = agent_id or conn.connected_by
    await save_tokens(session, conn, tokens)
    if new:
        for m in default_mappings(provider, conn.settings.get("contact_object", "Contact")):
            session.add(IntegrationMapping(connection_id=conn.id, **m))
    await session.commit()
    return conn


async def save_tokens(session: AsyncSession, conn: IntegrationConnection, tokens: dict) -> None:
    if tokens.get("access_token"):
        conn.access_token_secret_id = await put_secret(
            session, tokens["access_token"], f"integration:{conn.id}:access", conn.access_token_secret_id)
    if tokens.get("refresh_token"):
        conn.refresh_token_secret_id = await put_secret(
            session, tokens["refresh_token"], f"integration:{conn.id}:refresh", conn.refresh_token_secret_id)
    expires_in = tokens.get("expires_in")
    # Salesforce no informa expires_in: se refresca al recibir 401
    conn.expires_at = utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None


async def refresh_tokens(session: AsyncSession, conn: IntegrationConnection) -> None:
    s = get_settings()
    refresh_token = await get_secret(session, conn.refresh_token_secret_id)
    if not refresh_token:
        raise CRMError("No hay refresh token: vuelve a conectar la cuenta", retryable=False, status=401)
    if conn.provider == "hubspot":
        tokens = await hubspot.refresh(s.hubspot_client_id, s.hubspot_client_secret, refresh_token)
    elif conn.provider == "zoho":
        from app.hub import zoho

        tokens = await zoho.refresh(s.zoho_client_id, s.zoho_client_secret, refresh_token)
        tokens.pop("refresh_token", None)  # Zoho no rota el refresh token
        if tokens.get("api_domain"):
            conn.instance_url = tokens["api_domain"]
    else:
        tokens = await salesforce.refresh(s.salesforce_login_url, s.salesforce_client_id, s.salesforce_client_secret,
                                          refresh_token)
        if tokens.get("instance_url"):
            conn.instance_url = tokens["instance_url"]
    await save_tokens(session, conn, tokens)
    await session.flush()


async def adapter_for(session: AsyncSession, conn: IntegrationConnection, force_refresh: bool = False) -> CRMAdapter:
    if conn.provider == "custom":
        from app.hub import builder

        defn, client = await builder.load(session, conn)
        return builder.CustomRestAdapter(defn, client)
    if conn.provider == "odoo":  # clave de API (sin OAuth): instance_url = URL, settings.db / settings.login
        from app.hub import odoo

        key = await get_secret(session, conn.access_token_secret_id)
        s = conn.settings or {}
        if not key or not conn.instance_url:
            raise CRMError("Faltan las credenciales de Odoo", retryable=False, status=401)
        return odoo.OdooAdapter(conn.instance_url, s.get("db") or "", s.get("login") or "", key)
    if force_refresh or (conn.expires_at and conn.expires_at - utcnow() < timedelta(minutes=2)
                         and conn.refresh_token_secret_id):
        await refresh_tokens(session, conn)
    token = await get_secret(session, conn.access_token_secret_id)
    if not token:
        raise CRMError("La conexión no tiene token", retryable=False, status=401)
    if conn.provider == "hubspot":
        return hubspot.HubSpotAdapter(token)
    if conn.provider == "zoho":
        from app.hub import zoho

        return zoho.ZohoAdapter(token, conn.instance_url or "")
    return salesforce.SalesforceAdapter(token, conn.instance_url or "", (conn.settings or {}).get("contact_object", "Contact"))


async def disconnect(session: AsyncSession, conn: IntegrationConnection) -> None:
    s = get_settings()
    refresh_token = await get_secret(session, conn.refresh_token_secret_id)
    access_token = await get_secret(session, conn.access_token_secret_id)
    try:  # revocar es de buena educación; si falla, igual se borran los secretos
        if conn.provider == "hubspot" and refresh_token:
            await hubspot.revoke(refresh_token)
        elif conn.provider == "salesforce" and (refresh_token or access_token):
            await salesforce.revoke(s.salesforce_login_url, refresh_token or access_token)
        elif conn.provider == "zoho" and refresh_token:
            from app.hub import zoho

            await zoho.revoke(refresh_token)
    except Exception:  # noqa: BLE001
        pass
    await delete_secret(session, conn.access_token_secret_id)
    await delete_secret(session, conn.refresh_token_secret_id)
    await session.execute(delete(ExternalLink).where(ExternalLink.connection_id == conn.id))
    await session.delete(conn)
    await session.commit()
