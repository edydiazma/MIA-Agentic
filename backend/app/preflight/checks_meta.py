"""Meta: app, tokens, números de WhatsApp, páginas de Messenger/Instagram y cuenta publicitaria."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models import Channel, IntegrationConnection
from app.preflight.core import Context, Result, check, error_text, mask, skipped
from app.secrets_vault import get_secret

WA_SCOPES = ("whatsapp_business_messaging", "whatsapp_business_management")
PAGE_FIELDS = ("messages", "messaging_postbacks", "messaging_referrals")


def graph(ctx: Context) -> str:
    return f"https://graph.facebook.com/{ctx.settings.wa_api_version}"


def app_token(ctx: Context) -> str | None:
    s = ctx.settings
    return f"{s.meta_app_id}|{s.meta_app_secret}" if s.meta_app_id and s.meta_app_secret else None


async def debug_token(ctx: Context, http, token: str) -> dict | None:
    """Datos del token (validez, scopes, vencimiento) o None si no hay app para consultarlo."""
    at = app_token(ctx)
    if not at:
        return None
    r = await http.get(f"{graph(ctx)}/debug_token", params={"input_token": token, "access_token": at})
    if r.status_code >= 400:
        return {"is_valid": False, "error": error_text(r)}
    return (r.json() or {}).get("data") or {}


def token_result(area: str, key: str, label: str, info: dict | None, token: str, needed: tuple[str, ...],
                 fix: str) -> Result:
    data = {"token": mask(token)}
    if info is None:
        return Result(area, key, label, "skipped",
                      "Sin META_APP_ID / META_APP_SECRET no se puede inspeccionar el token (debug_token).", data)
    if not info.get("is_valid"):
        return Result(area, key, label, "fail",
                      f"El token no es válido ({info.get('error') or 'vencido o revocado'}). {fix}", data)
    scopes = set(info.get("scopes") or [])
    data.update({"scopes": sorted(scopes), "type": info.get("type"), "app_id": info.get("app_id")})
    missing = [sc for sc in needed if sc not in scopes]
    if missing:
        return Result(area, key, label, "fail", f"Al token le faltan permisos: {', '.join(missing)}. {fix}", data)
    exp = info.get("expires_at") or 0
    if exp:
        when = datetime.fromtimestamp(exp, UTC)
        data["expires_at"] = when.isoformat()
        return Result(area, key, label, "warn",
                      f"El token vence el {when:%Y-%m-%d}. Usa un token permanente de Usuario del sistema (Business "
                      "Manager → Usuarios del sistema → Generar token, sin vencimiento).", data)
    return Result(area, key, label, "pass", "Token válido, permanente y con los permisos necesarios.", data)


# --- Plataforma -------------------------------------------------------------------------------------------
@check("meta", "meta.app")
async def meta_app(ctx: Context) -> list[Result]:
    label = "App de Meta (META_APP_ID / META_APP_SECRET)"
    s = ctx.settings
    if not (s.meta_app_id and s.meta_app_secret):
        return skipped("meta", "meta.app", label,
                       "Sin app de Meta configurada: no habrá Embedded Signup ni verificación de tokens. Defínelas "
                       "con los datos de developers.facebook.com → tu app → Configuración → Básica.")
    async with ctx.http() as http:
        r = await http.get(f"{graph(ctx)}/{s.meta_app_id}", params={"fields": "name", "access_token": app_token(ctx)})
    if r.status_code >= 400:
        return [Result("meta", "meta.app", label, "fail",
                       f"Meta rechazó las credenciales de la app ({error_text(r)}): revisa el App ID y el App Secret.")]
    out = [Result("meta", "meta.app", label, "pass", f"App «{r.json().get('name')}» verificada.",
                  {"app_id": s.meta_app_id})]
    out.append(Result("meta", "meta.embedded_signup", "Embedded Signup (META_EMBEDDED_SIGNUP_CONFIG_ID)",
                      "pass" if s.meta_embedded_signup_config_id else "warn",
                      "Configurado." if s.meta_embedded_signup_config_id else
                      "Sin configuración de Embedded Signup: tus clientes no podrán conectar su número desde el "
                      "asistente. Créala en tu app → Facebook Login para empresas → Configuraciones."))
    return out


@check("meta", "meta.webhook")
async def meta_webhook(ctx: Context) -> list[Result]:
    s = ctx.settings
    out = [Result("meta", "meta.verify_token", "Token de verificación del webhook (WA_VERIFY_TOKEN)",
                  "fail" if s.wa_verify_token in ("", "verify-me") else "pass",
                  "Cambia WA_VERIFY_TOKEN por un valor aleatorio y regístralo en Meta (WhatsApp → Configuración → "
                  "Webhook)." if s.wa_verify_token in ("", "verify-me") else "Definido.")]
    out.append(Result("meta", "meta.app_secret", "Firma de los webhooks (WA_APP_SECRET)",
                      "pass" if s.wa_app_secret else "fail",
                      "Definido: se valida la firma X-Hub-Signature-256." if s.wa_app_secret else
                      "Sin WA_APP_SECRET no se valida la firma de los webhooks: cualquiera podría inyectar mensajes. "
                      "Usa el App Secret de tu app."))
    return out


@check("meta", "meta.server_tokens")
async def server_tokens(ctx: Context) -> list[Result]:
    s = ctx.settings
    out = []
    async with ctx.http() as http:
        if s.wa_access_token:
            info = await debug_token(ctx, http, s.wa_access_token)
            out.append(token_result("meta", "meta.wa_access_token", "Token de WhatsApp del servidor (WA_ACCESS_TOKEN)",
                                    info, s.wa_access_token, WA_SCOPES,
                                    "Genera un token de Usuario del sistema con whatsapp_business_messaging y "
                                    "whatsapp_business_management."))
        if s.meta_capi_token:
            info = await debug_token(ctx, http, s.meta_capi_token)
            out.append(token_result("meta", "meta.capi_token", "Token de Conversions API (META_CAPI_TOKEN)", info,
                                    s.meta_capi_token, (), "Genéralo en Events Manager → tu dataset → Configuración."))
    return out or skipped("meta", "meta.server_tokens", "Tokens de Meta del servidor",
                          "Sin WA_ACCESS_TOKEN ni META_CAPI_TOKEN: cada empresa usa los tokens de sus canales.")


# --- Empresa: canales -------------------------------------------------------------------------------------
async def _channel_token(ctx: Context, s, c: Channel) -> str | None:
    return await get_secret(s, c.access_token_secret_id) or (ctx.settings.wa_access_token or None)


@check("meta", "meta.whatsapp_channels", scope="org")
async def whatsapp_channels(ctx: Context) -> list[Result]:
    async with ctx.db() as s:
        channels = (await s.scalars(select(Channel).where(
            Channel.organization_id == ctx.organization_id, Channel.provider == "whatsapp_cloud"))).unique().all()
        tokens = {c.id: await _channel_token(ctx, s, c) for c in channels}
    if not channels:
        return skipped("meta", "meta.whatsapp_channels", "Números de WhatsApp",
                       "La empresa no tiene números de WhatsApp: conéctalo en Configuraciones → Asistente.")
    out: list[Result] = []
    async with ctx.http() as http:
        for c in channels:
            name = c.name or c.display_phone or c.phone_number_id
            sfx = f":{c.id}"
            token = tokens[c.id]
            if not token:
                out.append(Result("meta", f"meta.wa_token{sfx}", f"{name}: token", "fail",
                                  "El número no tiene token: vuelve a conectarlo (Embedded Signup) o guarda un token "
                                  "permanente en Configuraciones → Plataforma.", {"channel_id": c.id}))
                continue
            info = await debug_token(ctx, http, token)
            out.append(token_result("meta", f"meta.wa_token{sfx}", f"{name}: token", info, token, WA_SCOPES,
                                    "Reconecta el número con Embedded Signup o usa un token de Usuario del sistema."))
            auth = {"Authorization": f"Bearer {token}"}
            # Estado del número
            r = await http.get(f"{graph(ctx)}/{c.phone_number_id}", headers=auth, params={
                "fields": "verified_name,code_verification_status,display_phone_number,quality_rating,"
                          "platform_type,throughput,name_status"})
            if r.status_code >= 400:
                out.append(Result("meta", f"meta.wa_phone{sfx}", f"{name}: estado del número", "fail",
                                  f"No se pudo leer el número ({error_text(r)}): el token no tiene acceso a este "
                                  "phone_number_id.", {"channel_id": c.id}))
            else:
                p = r.json()
                problems = []
                if p.get("platform_type") not in (None, "CLOUD_API"):
                    problems.append(f"plataforma {p.get('platform_type')} (debe ser CLOUD_API: regístralo con PIN)")
                if p.get("quality_rating") in ("RED", "YELLOW"):
                    problems.append(f"calidad {p.get('quality_rating')}")
                if p.get("name_status") not in (None, "APPROVED", "AVAILABLE_WITHOUT_REVIEW"):
                    problems.append(f"nombre visible {p.get('name_status')}")
                status = "fail" if p.get("platform_type") not in (None, "CLOUD_API") else (
                    "warn" if problems else "pass")
                out.append(Result("meta", f"meta.wa_phone{sfx}", f"{name}: estado del número", status,
                                  ("Problemas: " + "; ".join(problems) + ". Revísalo en WhatsApp Manager.")
                                  if problems else "Registrado en Cloud API, calidad y nombre en orden.",
                                  {"channel_id": c.id, **{k: p.get(k) for k in (
                                      "display_phone_number", "verified_name", "quality_rating", "platform_type",
                                      "name_status", "code_verification_status")},
                                   "throughput": (p.get("throughput") or {}).get("level")}))
            # Webhook: la app suscrita a la cuenta de WhatsApp Business
            if c.waba_id:
                r = await http.get(f"{graph(ctx)}/{c.waba_id}/subscribed_apps", headers=auth)
                apps = [((a.get("whatsapp_business_api_data") or {}).get("id") or a.get("id"))
                        for a in (r.json().get("data") or [])] if r.status_code < 400 else []
                ours = ctx.settings.meta_app_id
                ok = r.status_code < 400 and (ours in apps if ours else bool(apps))
                out.append(Result("meta", f"meta.wa_subscribed{sfx}", f"{name}: webhook suscrito", "pass" if ok else "fail",
                                  "La app recibe los eventos de esta cuenta." if ok else
                                  "La app no está suscrita a la cuenta de WhatsApp Business: no llegarán mensajes. "
                                  "Usa «Arreglar» en el asistente o POST /{waba_id}/subscribed_apps.",
                                  {"channel_id": c.id, "apps": [a for a in apps if a]}))
                r = await http.get(f"{graph(ctx)}/{c.waba_id}/message_templates", headers=auth,
                                   params={"limit": 1, "fields": "name,status"})
                out.append(Result("meta", f"meta.wa_templates{sfx}", f"{name}: plantillas",
                                  "pass" if r.status_code < 400 else "fail",
                                  "Se pueden leer las plantillas." if r.status_code < 400 else
                                  f"No se pueden leer las plantillas ({error_text(r)}): el token necesita "
                                  "whatsapp_business_management sobre esta cuenta.", {"channel_id": c.id}))
            else:
                out.append(Result("meta", f"meta.wa_subscribed{sfx}", f"{name}: webhook suscrito", "warn",
                                  "El canal no tiene WABA ID: guárdalo para verificar el webhook y usar plantillas.",
                                  {"channel_id": c.id}))
            if getattr(c, "calling_enabled", False):
                r = await http.get(f"{graph(ctx)}/{c.phone_number_id}/settings", headers=auth)
                calling = ((r.json() or {}).get("calling") or {}) if r.status_code < 400 else {}
                enabled = str(calling.get("status", "")).upper() == "ENABLED"
                out.append(Result("meta", f"meta.wa_calling{sfx}", f"{name}: llamadas", "pass" if enabled else "fail",
                                  "Calling API habilitada." if enabled else
                                  "Las llamadas están activas en el panel pero no en Meta: habilítalas en WhatsApp "
                                  "Manager → Números → Llamadas, o con POST /{phone_number_id}/settings.",
                                  {"channel_id": c.id, "status": calling.get("status")}))
    return out


@check("meta", "meta.page_channels", scope="org")
async def page_channels(ctx: Context) -> list[Result]:
    async with ctx.db() as s:
        channels = (await s.scalars(select(Channel).where(
            Channel.organization_id == ctx.organization_id,
            Channel.provider.in_(("messenger", "instagram"))))).unique().all()
        tokens = {c.id: await get_secret(s, c.access_token_secret_id) for c in channels}
    if not channels:
        return skipped("meta", "meta.page_channels", "Messenger e Instagram", "La empresa no tiene estos canales.")
    out: list[Result] = []
    async with ctx.http() as http:
        for c in channels:
            sfx, name = f":{c.id}", c.name
            token, page = tokens[c.id], c.page_id or c.external_id
            if not token:
                out.append(Result("meta", f"meta.page_token{sfx}", f"{name}: token de página", "fail",
                                  "El canal no tiene token de página: vuelve a conectarlo.", {"channel_id": c.id}))
                continue
            r = await http.get(f"{graph(ctx)}/me", params={"fields": "id,name", "access_token": token})
            out.append(Result("meta", f"meta.page_token{sfx}", f"{name}: token de página",
                              "pass" if r.status_code < 400 else "fail",
                              "Token de página válido." if r.status_code < 400 else
                              f"Token inválido ({error_text(r)}): genera un token de página que no venza (Usuario del "
                              "sistema con pages_messaging).", {"channel_id": c.id, "token": mask(token)}))
            if r.status_code >= 400:
                continue
            r = await http.get(f"{graph(ctx)}/{page}/subscribed_apps", params={"access_token": token})
            fields = set()
            for a in (r.json().get("data") or []) if r.status_code < 400 else []:
                fields |= set(a.get("subscribed_fields") or [])
            missing = [f for f in PAGE_FIELDS if f not in fields]
            out.append(Result("meta", f"meta.page_subscribed{sfx}", f"{name}: webhook de la página",
                              "pass" if not missing else "fail",
                              "La página envía mensajes, postbacks y referidos." if not missing else
                              f"Faltan campos suscritos: {', '.join(missing)}. Guarda el canal de nuevo para "
                              "suscribirlo o hazlo en tu app → Messenger → Configuración.",
                              {"channel_id": c.id, "fields": sorted(fields)}))
            if c.provider == "instagram":
                r = await http.get(f"{graph(ctx)}/{page}", params={"fields": "instagram_business_account",
                                                                   "access_token": token})
                ig = (r.json().get("instagram_business_account") or {}).get("id") if r.status_code < 400 else None
                out.append(Result("meta", f"meta.ig_linked{sfx}", f"{name}: cuenta de Instagram",
                                  "pass" if ig else "fail",
                                  "Cuenta profesional de Instagram vinculada." if ig else
                                  "La página no tiene una cuenta profesional de Instagram vinculada: vincúlala en "
                                  "Meta Business Suite → Configuración → Cuentas de Instagram.",
                                  {"channel_id": c.id, "ig_account_id": ig}))
    return out


@check("meta", "meta.ads", scope="org")
async def meta_ads(ctx: Context) -> list[Result]:
    label = "Cuenta publicitaria de Meta (ads_read)"
    async with ctx.db() as s:
        conn = await s.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == ctx.organization_id, IntegrationConnection.provider == "meta"))
        token = (await get_secret(s, conn.access_token_secret_id) if conn else None) or ctx.settings.meta_capi_token
    account = ((conn.settings or {}) if conn else {}).get("ad_account_id")
    if not conn or not account:
        return skipped("meta", "meta.ads", label,
                       "Sin cuenta publicitaria configurada: agrégala en Configuraciones → Conversiones (Meta) para ver "
                       "nombres de anuncios e inversión.")
    if not token:
        return [Result("meta", "meta.ads", label, "fail", "La conexión de Meta no tiene token.")]
    async with ctx.http() as http:
        r = await http.get(f"{graph(ctx)}/act_{account}", params={"fields": "name,account_status,currency",
                                                                  "access_token": token})
    if r.status_code >= 400:
        return [Result("meta", "meta.ads", label, "fail",
                       f"No se pudo leer la cuenta act_{account} ({error_text(r)}): el token necesita ads_read y "
                       "acceso a esa cuenta en Business Manager.", {"ad_account_id": account})]
    a = r.json()
    active = a.get("account_status") == 1
    return [Result("meta", "meta.ads", label, "pass" if active else "warn",
                   f"Cuenta «{a.get('name')}» ({a.get('currency')})." + ("" if active else
                                                                         " La cuenta no está activa en Meta."),
                   {"ad_account_id": account, "status": a.get("account_status"), "currency": a.get("currency")})]
