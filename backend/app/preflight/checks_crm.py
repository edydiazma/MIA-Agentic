"""Google Ads, HubSpot y Salesforce. Los tokens se renuevan solo EN MEMORIA (no se guardan): es una prueba."""

from sqlalchemy import select

from app.models import IntegrationConnection, utcnow
from app.preflight.core import Context, Result, check, error_text, mask, skipped
from app.secrets_vault import get_secret

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ADS_API = "https://googleads.googleapis.com/v20"
HUBSPOT_API = "https://api.hubapi.com"
HUBSPOT_SCOPES = ("crm.objects.contacts.read", "crm.objects.contacts.write", "crm.objects.deals.read",
                  "crm.objects.deals.write")
SALESFORCE_API_VERSION = "v61.0"


async def _conn(ctx: Context, provider: str) -> tuple[IntegrationConnection | None, str | None, str | None]:
    async with ctx.db() as s:
        conn = await s.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == ctx.organization_id, IntegrationConnection.provider == provider))
        if not conn:
            return None, None, None
        return conn, await get_secret(s, conn.access_token_secret_id), await get_secret(s, conn.refresh_token_secret_id)


def _fresh(conn: IntegrationConnection, access: str | None) -> bool:
    return bool(access and conn.expires_at and conn.expires_at > utcnow())


# --- Google Ads -------------------------------------------------------------------------------------------
@check("google_ads", "google_ads.config")
async def google_config(ctx: Context) -> list[Result]:
    s = ctx.settings
    if not (s.google_oauth_client_id and s.google_oauth_client_secret):
        return skipped("google_ads", "google_ads.config", "Google Ads (credenciales del servidor)",
                       "Sin GOOGLE_OAUTH_CLIENT_ID/SECRET: las empresas no podrán conectar Google Ads.")
    return [Result("google_ads", "google_ads.config", "Google Ads (credenciales del servidor)",
                   "pass" if s.google_ads_developer_token else "fail",
                   "Cliente OAuth y developer token definidos." if s.google_ads_developer_token else
                   "Falta GOOGLE_ADS_DEVELOPER_TOKEN (Google Ads → Herramientas → Centro de API). Sin él no se suben "
                   "conversiones ni se lee la inversión.")]


@check("google_ads", "google_ads.connection", scope="org")
async def google_connection(ctx: Context) -> list[Result]:
    label = "Google Ads (cuenta conectada)"
    conn, _access, refresh = await _conn(ctx, "google_ads")
    if not conn:
        return skipped("google_ads", "google_ads.connection", label, "La empresa no conectó Google Ads.")
    s = ctx.settings
    if not refresh:
        return [Result("google_ads", "google_ads.connection", label, "fail",
                       "La conexión no tiene refresh token: vuelve a conectar Google Ads en Configuraciones → "
                       "Conversiones.")]
    out: list[Result] = []
    async with ctx.http() as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={"client_id": s.google_oauth_client_id,
                                                    "client_secret": s.google_oauth_client_secret,
                                                    "refresh_token": refresh, "grant_type": "refresh_token"})
        if r.status_code >= 400:
            return [Result("google_ads", "google_ads.oauth", "Google Ads: autorización", "fail",
                           f"Google rechazó el refresh token ({error_text(r)}): vuelve a conectar la cuenta.")]
        token = r.json().get("access_token", "")
        out.append(Result("google_ads", "google_ads.oauth", "Google Ads: autorización", "pass",
                          "La autorización se renueva correctamente.", {"token": mask(token)}))
        if not s.google_ads_developer_token:
            out.append(Result("google_ads", "google_ads.developer_token", "Google Ads: developer token", "fail",
                              "Falta GOOGLE_ADS_DEVELOPER_TOKEN en el servidor."))
            return out
        h = {"Authorization": f"Bearer {token}", "developer-token": s.google_ads_developer_token}
        r = await http.get(f"{GOOGLE_ADS_API}/customers:listAccessibleCustomers", headers=h)
        out.append(Result("google_ads", "google_ads.developer_token", "Google Ads: developer token",
                          "pass" if r.status_code < 400 else "fail",
                          "Developer token aceptado." if r.status_code < 400 else
                          f"Google Ads rechazó el developer token ({error_text(r)}): en modo «prueba» solo funciona "
                          "con cuentas de prueba; solicita acceso básico."))
        cid = str(conn.external_account_id or "").replace("-", "")
        if r.status_code >= 400 or not cid:
            if not cid:
                out.append(Result("google_ads", "google_ads.customer", "Google Ads: cuenta de cliente", "fail",
                                  "No se eligió la cuenta (customer id): selecciónala en Configuraciones → "
                                  "Conversiones."))
            return out
        login = (conn.settings or {}).get("login_customer_id") or s.google_ads_login_customer_id
        if login:
            h["login-customer-id"] = str(login).replace("-", "")
        q = "SELECT customer.id, customer.descriptive_name, customer.currency_code FROM customer LIMIT 1"
        r = await http.post(f"{GOOGLE_ADS_API}/customers/{cid}/googleAds:search", headers=h, json={"query": q})
        if r.status_code >= 400:
            out.append(Result("google_ads", "google_ads.customer", "Google Ads: cuenta de cliente", "fail",
                              f"No se pudo leer la cuenta {cid} ({error_text(r)}): si la gestionas desde una MCC "
                              "define el login-customer-id.", {"customer_id": cid}))
            return out
        row = ((r.json().get("results") or [{}])[0]).get("customer") or {}
        out.append(Result("google_ads", "google_ads.customer", "Google Ads: cuenta de cliente", "pass",
                          f"Cuenta «{row.get('descriptiveName', cid)}» ({row.get('currencyCode', '?')}).",
                          {"customer_id": cid}))
        q = "SELECT conversion_action.id, conversion_action.name FROM conversion_action LIMIT 50"
        r = await http.post(f"{GOOGLE_ADS_API}/customers/{cid}/googleAds:search", headers=h, json={"query": q})
        n = len(r.json().get("results") or []) if r.status_code < 400 else 0
        out.append(Result("google_ads", "google_ads.conversion_actions", "Google Ads: acciones de conversión",
                          "pass" if n else "warn",
                          f"{n} acciones de conversión disponibles." if n else
                          "No hay acciones de conversión: crea una de tipo «Importación» para subir ventas de WhatsApp.",
                          {"count": n}))
    return out


# --- HubSpot ----------------------------------------------------------------------------------------------
@check("hubspot", "hubspot.connection", scope="org")
async def hubspot(ctx: Context) -> list[Result]:
    label = "HubSpot"
    conn, access, refresh = await _conn(ctx, "hubspot")
    if not conn:
        return skipped("hubspot", "hubspot.connection", label, "La empresa no conectó HubSpot.")
    s = ctx.settings
    async with ctx.http() as http:
        token = access
        if not _fresh(conn, access) and refresh and s.hubspot_client_id:
            r = await http.post(f"{HUBSPOT_API}/oauth/v1/token", data={
                "grant_type": "refresh_token", "client_id": s.hubspot_client_id,
                "client_secret": s.hubspot_client_secret, "refresh_token": refresh})
            if r.status_code >= 400:
                return [Result("hubspot", "hubspot.connection", label, "fail",
                               f"HubSpot rechazó el refresh token ({error_text(r)}): vuelve a conectar la cuenta.")]
            token = r.json().get("access_token")
        if not token:
            return [Result("hubspot", "hubspot.connection", label, "fail",
                           "La conexión no tiene token: vuelve a conectar HubSpot.")]
        r = await http.get(f"{HUBSPOT_API}/oauth/v1/access-tokens/{token}")
        if r.status_code >= 400:
            # Token de app privada: no se puede introspectar, se prueba una lectura mínima
            r2 = await http.get(f"{HUBSPOT_API}/crm/v3/objects/contacts", params={"limit": 1},
                                headers={"Authorization": f"Bearer {token}"})
            return [Result("hubspot", "hubspot.connection", label, "pass" if r2.status_code < 400 else "fail",
                           "Token válido (lectura de contactos)." if r2.status_code < 400 else
                           f"HubSpot rechazó el token ({error_text(r2)}).", {"token": mask(token)})]
    info = r.json()
    scopes = set(info.get("scopes") or [])
    missing = [sc for sc in HUBSPOT_SCOPES if sc not in scopes]
    return [Result("hubspot", "hubspot.connection", label, "fail" if missing else "pass",
                   f"Faltan permisos: {', '.join(missing)}. Reconecta HubSpot aceptando esos permisos."
                   if missing else f"Portal {info.get('hub_id')} conectado con los permisos necesarios.",
                   {"hub_id": info.get("hub_id"), "scopes": sorted(scopes), "token": mask(token)})]


# --- Salesforce -------------------------------------------------------------------------------------------
@check("salesforce", "salesforce.connection", scope="org")
async def salesforce(ctx: Context) -> list[Result]:
    label = "Salesforce"
    conn, access, refresh = await _conn(ctx, "salesforce")
    if not conn:
        return skipped("salesforce", "salesforce.connection", label, "La empresa no conectó Salesforce.")
    s = ctx.settings
    instance = conn.instance_url
    async with ctx.http() as http:
        token = access
        if refresh and s.salesforce_client_id:
            r = await http.post(f"{s.salesforce_login_url.rstrip('/')}/services/oauth2/token", data={
                "grant_type": "refresh_token", "client_id": s.salesforce_client_id,
                "client_secret": s.salesforce_client_secret, "refresh_token": refresh})
            if r.status_code >= 400:
                return [Result("salesforce", "salesforce.connection", label, "fail",
                               f"Salesforce rechazó el refresh token ({error_text(r)}): vuelve a conectar la org o "
                               "revisa la política de refresh token de la Connected App.")]
            data = r.json()
            token, instance = data.get("access_token"), data.get("instance_url") or instance
        if not (token and instance):
            return [Result("salesforce", "salesforce.connection", label, "fail",
                           "La conexión no tiene token o instance_url: vuelve a conectar Salesforce.")]
        r = await http.get(f"{instance.rstrip('/')}/services/data/{SALESFORCE_API_VERSION}/limits",
                           headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        return [Result("salesforce", "salesforce.connection", label, "fail",
                       f"No se pudo consultar la org ({error_text(r)}): el usuario integrado necesita «API Enabled».",
                       {"instance": instance})]
    daily = r.json().get("DailyApiRequests") or {}
    mx, rem = daily.get("Max") or 0, daily.get("Remaining") or 0
    pct = round(100 * rem / mx, 1) if mx else None
    status = "pass" if pct is None or pct >= 10 else "warn"
    return [Result("salesforce", "salesforce.connection", label, status,
                   f"Org conectada; quedan {rem} de {mx} llamadas API hoy." + (
                       "" if status == "pass" else " Quedan pocas llamadas: la sincronización podría pausarse."),
                   {"instance": instance, "api_remaining": rem, "api_max": mx})]
