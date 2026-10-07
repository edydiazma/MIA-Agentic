"""Enriquecimiento de atribuciones con nombres de campaña / conjunto / anuncio (Meta) y de clic (Google Ads).

- Meta: el ad_id del referral de Click to WhatsApp → GET /{ad_id}?fields=name,adset{id,name},campaign{id,name}.
- Google Ads: el gclid → click_view (GAQL; exige filtrar un solo día y solo conserva 90 días).
Las respuestas se guardan en ad_entities (caché por organización; 7 días para anuncios, sin vencimiento para
clics: un gclid no cambia de campaña). enrich_soon() corre al llegar el mensaje; enrichment_loop() reintenta.
Nunca lanza errores al llamador: un fallo deja enrichment_status = 'failed'.
"""

import asyncio
import logging
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import conversions
from app.config import get_settings
from app.db import SessionLocal
from app.models import AdEntity, Attribution, IntegrationConnection, utcnow
from app.secrets_vault import get_secret

log = logging.getLogger(__name__)
AD_CACHE = timedelta(days=7)
CLICK_VIEW_DAYS = 90
_tasks: set[asyncio.Task] = set()


class EnrichError(Exception):
    pass


async def connection(session: AsyncSession, org: int, provider: str) -> IntegrationConnection | None:
    return (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider == provider))).first()


async def meta_token(session: AsyncSession, org: int) -> str | None:
    """Mismo orden que conversions.upload_meta: token del servidor o el de la conexión Meta."""
    conn = await connection(session, org, "meta")
    return get_settings().meta_capi_token or (await get_secret(session, conn.access_token_secret_id) if conn else None)


async def google_search(session: AsyncSession, conn: IntegrationConnection, query: str) -> list[dict]:
    """googleAds:search con los mismos encabezados que la subida de conversiones."""
    env = get_settings()
    cid = (conn.external_account_id or "").replace("-", "")
    if not cid:
        raise EnrichError("Google Ads no tiene cuenta (customer id) configurada")
    if not env.google_ads_developer_token:
        raise EnrichError("Falta GOOGLE_ADS_DEVELOPER_TOKEN en el servidor")
    try:
        token = await conversions.google_access_token(session, conn)
    except conversions.UploadError as e:
        raise EnrichError(str(e)) from e
    headers = {"Authorization": f"Bearer {token}", "developer-token": env.google_ads_developer_token}
    login = (conn.settings or {}).get("login_customer_id") or env.google_ads_login_customer_id
    if login:
        headers["login-customer-id"] = str(login).replace("-", "")
    async with conversions.http_client(30) as http:
        r = await http.post(f"{conversions.GOOGLE_ADS_API}/customers/{cid}/googleAds:search", headers=headers,
                            json={"query": query})
    if r.status_code >= 400:
        raise EnrichError(f"Google Ads {r.status_code}: {r.text[:300]}")
    return (r.json() or {}).get("results") or []


async def meta_get(path: str, token: str, params: dict) -> dict:
    async with conversions.http_client(30) as http:
        r = await http.get(f"{conversions.META_GRAPH}/{path}", params={**params, "access_token": token})
    if r.status_code >= 400:
        raise EnrichError(f"Meta {r.status_code}: {r.text[:300]}")
    return r.json() or {}


async def _entity(session: AsyncSession, org: int, platform: str, etype: str, ext_id: str) -> AdEntity | None:
    return await session.scalar(select(AdEntity).where(
        AdEntity.organization_id == org, AdEntity.platform == platform, AdEntity.entity_type == etype,
        AdEntity.external_id == ext_id))


async def _meta_ad(session: AsyncSession, org: int, ad_id: str) -> AdEntity:
    row = await _entity(session, org, "meta", "ad", ad_id)
    if row and row.fetched_at >= utcnow() - AD_CACHE:
        return row
    token = await meta_token(session, org)
    if not token:
        raise EnrichError("Falta el token de Meta (META_CAPI_TOKEN o conexión Meta)")
    data = await meta_get(ad_id, token, {"fields": "name,adset{id,name},campaign{id,name}"})
    row = row or AdEntity(organization_id=org, platform="meta", entity_type="ad", external_id=ad_id)
    adset, campaign = data.get("adset") or {}, data.get("campaign") or {}
    row.name, row.data, row.fetched_at = data.get("name"), data, utcnow()
    row.campaign_id, row.campaign_name = campaign.get("id"), campaign.get("name")
    row.ad_group_id, row.ad_group_name = adset.get("id"), adset.get("name")
    session.add(row)
    return row


async def _google_click(session: AsyncSession, attr: Attribution) -> AdEntity:
    org, gclid = attr.organization_id, attr.gclid
    row = await _entity(session, org, "google_ads", "click", gclid)
    if row:
        return row
    conn = await connection(session, org, "google_ads")
    if not conn or conn.status != "connected":
        raise EnrichError("Google Ads no está conectado")
    clicked = (attr.web_session_at or attr.created_at or utcnow()).astimezone(UTC)
    if clicked < utcnow() - timedelta(days=CLICK_VIEW_DAYS):
        raise EnrichError("El clic tiene más de 90 días: Google Ads ya no lo reporta")
    safe = gclid.replace("'", "").replace("\\", "")
    results = await google_search(session, conn, (
        "SELECT click_view.gclid, campaign.id, campaign.name, ad_group.id, ad_group.name, "
        "click_view.keyword_info.text FROM click_view "
        f"WHERE segments.date = '{clicked.date().isoformat()}' AND click_view.gclid = '{safe}'"))
    if not results:
        raise EnrichError("Google Ads no encontró el clic (gclid) en esa fecha")
    res = results[0]
    campaign, group = res.get("campaign") or {}, res.get("adGroup") or {}
    keyword = ((res.get("clickView") or {}).get("keywordInfo") or {}).get("text")
    row = AdEntity(organization_id=org, platform="google_ads", entity_type="click", external_id=gclid,
                   campaign_id=str(campaign["id"]) if campaign.get("id") else None, campaign_name=campaign.get("name"),
                   ad_group_id=str(group["id"]) if group.get("id") else None, ad_group_name=group.get("name"),
                   keyword=keyword, data=res, fetched_at=utcnow())
    session.add(row)
    return row


async def enrich(session: AsyncSession, attr: Attribution) -> str:
    """Completa los nombres de campaña de la atribución. Devuelve done | failed | skipped (y lo guarda)."""
    attr_id = attr.id
    try:
        if attr.ad_id:
            ad = await _meta_ad(session, attr.organization_id, attr.ad_id)
            attr.ad_name, attr.platform_campaign_id, attr.platform_campaign_name = ad.name, ad.campaign_id, ad.campaign_name
            attr.ad_group_id, attr.ad_group_name = ad.ad_group_id, ad.ad_group_name
        elif attr.gclid:
            click = await _google_click(session, attr)
            attr.platform_campaign_id, attr.platform_campaign_name = click.campaign_id, click.campaign_name
            attr.ad_group_id, attr.ad_group_name, attr.keyword = click.ad_group_id, click.ad_group_name, click.keyword
        else:
            attr.enrichment_status = "skipped"
            await session.commit()
            return "skipped"
        attr.enrichment_status, attr.enriched_at = "done", utcnow()
        await session.commit()
    except Exception as e:  # red, token, permisos: queda registrado y no rompe la conversación
        await session.rollback()
        log.warning("No se pudo enriquecer la atribución %s: %s", attr_id, e)
        attr = await session.get(Attribution, attr_id)
        if attr is not None:
            attr.enrichment_status, attr.enriched_at = "failed", utcnow()
            await session.commit()
        return "failed"
    try:
        from app.crm.sync import enqueue_attribution  # contrato del módulo CRM (puede no existir aún)

        await enqueue_attribution(session, attr.contact_id)
        await session.commit()
    except (ImportError, AttributeError):
        pass
    except Exception:
        log.exception("No se pudo encolar la atribución %s hacia el CRM", attr_id)
    return "done"


async def enrich_id(attribution_id: int) -> str:
    async with SessionLocal() as session:
        attr = await session.get(Attribution, attribution_id)
        if not attr or attr.enrichment_status not in ("pending", "failed"):
            return "skipped"
        return await enrich(session, attr)


async def _safe(attribution_id: int) -> None:
    try:
        await enrich_id(attribution_id)
    except Exception:
        log.exception("Falló el enriquecimiento de la atribución %s", attribution_id)


def enrich_soon(attribution_id: int) -> None:
    """Enriquecimiento en segundo plano (no bloquea la respuesta al cliente)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_safe(attribution_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def enrich_pending(limit: int = 100) -> int:
    """Reintenta las atribuciones pendientes de más de 1 minuto (las recientes las atiende enrich_soon)."""
    async with SessionLocal() as session:
        ids = (await session.scalars(select(Attribution.id).where(
            Attribution.enrichment_status == "pending", Attribution.created_at <= utcnow() - timedelta(minutes=1))
            .order_by(Attribution.created_at).limit(limit))).all()
    for aid in ids:
        await enrich_id(aid)
    return len(ids)


async def enrichment_loop() -> None:
    """Registrar en main.py (lifespan): cada 5 minutos."""
    while True:
        await asyncio.sleep(300)
        try:
            done = await enrich_pending()
            if done:
                log.info("Enriquecimiento: %s atribuciones procesadas", done)
        except Exception:
            log.exception("Falló el ciclo de enriquecimiento")
