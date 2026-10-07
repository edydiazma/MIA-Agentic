"""Enriquecimiento de atribuciones con nombres de campaña / conjunto / anuncio (Meta) y de clic (Google Ads).

- Meta: el ad_id del referral de Click to WhatsApp → GET /{ad_id} con nombre, estado, conjunto, campaña y creativo
  (effective_object_story_id = "{page_id}_{post_id}" de la publicación del anuncio).
- Publicaciones (referral source_type = post, sin ad_id): se busca el anuncio que promocionó esa publicación en
  ad_entities (effective_object_story_id); si no está, se consulta la cuenta publicitaria. Una publicación orgánica
  queda como "Publicación". ads_catalog_loop() sincroniza los anuncios de la cuenta cada 6 h.
- Google Ads: el gclid → click_view (GAQL; exige filtrar un solo día y solo conserva 90 días).
Las respuestas se guardan en ad_entities (caché por organización; 7 días para anuncios, sin vencimiento para
clics: un gclid no cambia de campaña). enrich_soon() corre al llegar el mensaje; enrichment_loop() reintenta.
Nunca lanza errores al llamador: un fallo deja enrichment_status = 'failed'.
"""

import asyncio
import json
import logging
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import conversions
from app.config import get_settings
from app.db import SessionLocal
from app.models import AdEntity, Attribution, AttributionTouch, IntegrationConnection, utcnow
from app.secrets_vault import get_secret

log = logging.getLogger(__name__)
AD_CACHE = timedelta(days=7)
AD_FIELDS = ("name,effective_status,account_id,adset{id,name,destination_type},campaign{id,name},"
             "creative{effective_object_story_id,title,body,image_url,thumbnail_url,video_id,object_type}")
CATALOG_EVERY_S = 6 * 3600
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
    """GET al Graph API. `path` puede ser una URL completa (paging.next, que ya trae el token)."""
    async with conversions.http_client(30) as http:
        if path.startswith("http"):
            r = await http.get(path)
        else:
            r = await http.get(f"{conversions.META_GRAPH}/{path}", params={**params, "access_token": token})
    if r.status_code >= 400:
        raise EnrichError(f"Meta {r.status_code}: {r.text[:300]}")
    return r.json() or {}


def ad_account_id(conn: IntegrationConnection | None) -> str | None:
    acc = ((conn.settings or {}).get("ad_account_id") if conn else None) or None
    return str(acc).removeprefix("act_") if acc else None


def _store_meta_ad(row: AdEntity, data: dict) -> AdEntity:
    adset, campaign, creative = data.get("adset") or {}, data.get("campaign") or {}, data.get("creative") or {}
    row.name, row.data, row.fetched_at = data.get("name"), data, utcnow()
    row.campaign_id, row.campaign_name = campaign.get("id"), campaign.get("name")
    row.ad_group_id, row.ad_group_name = adset.get("id"), adset.get("name")
    row.status = data.get("effective_status")
    row.account_id = data.get("account_id")
    row.destination = (adset.get("destination_type") or "").lower() or None
    row.effective_object_story_id = creative.get("effective_object_story_id")
    row.headline, row.body = creative.get("title"), creative.get("body")
    row.media_type = "video" if creative.get("video_id") else ("image" if creative.get("image_url") else None)
    row.media_url = creative.get("image_url")
    row.thumbnail_url = creative.get("thumbnail_url") or creative.get("image_url")
    return row


async def _upsert_meta_ad(session: AsyncSession, org: int, data: dict) -> AdEntity:
    ad_id = str(data["id"])
    row = await _entity(session, org, "meta", "ad", ad_id) or AdEntity(
        organization_id=org, platform="meta", entity_type="ad", external_id=ad_id)
    session.add(_store_meta_ad(row, data))
    return row


async def sync_meta_ads(session: AsyncSession, org: int, max_pages: int = 25) -> int:
    """Copia los anuncios de la cuenta publicitaria a ad_entities (para cruzar publicaciones → anuncio)."""
    conn = await connection(session, org, "meta")
    account = ad_account_id(conn)
    if not account:
        raise EnrichError("Configura la cuenta publicitaria de Meta (act_…) en Conversiones")
    token = await meta_token(session, org)
    if not token:
        raise EnrichError("Falta el token de Meta (META_CAPI_TOKEN o conexión Meta)")
    path, params, count = f"act_{account}/ads", {"fields": f"id,{AD_FIELDS}", "limit": 200}, 0
    for _ in range(max_pages):
        page = await meta_get(path, token, params)
        for ad in page.get("data") or []:
            if ad.get("id"):
                await _upsert_meta_ad(session, org, ad)
                count += 1
        nxt = (page.get("paging") or {}).get("next")
        if not nxt:
            break
        path, params = nxt, {}
    await session.commit()
    return count


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
    data = await meta_get(ad_id, token, {"fields": AD_FIELDS})
    row = row or AdEntity(organization_id=org, platform="meta", entity_type="ad", external_id=ad_id)
    session.add(_store_meta_ad(row, data))
    return row


async def _local_ad_for_post(session: AsyncSession, org: int, post_id: str) -> AdEntity | None:
    return (await session.scalars(select(AdEntity).where(
        AdEntity.organization_id == org, AdEntity.platform == "meta", AdEntity.entity_type == "ad",
        AdEntity.effective_object_story_id == post_id)
        .order_by((AdEntity.status == "ACTIVE").desc(), AdEntity.fetched_at.desc()).limit(1))).first()


async def meta_ad_for_post(session: AsyncSession, org: int, post_id: str) -> AdEntity | None:
    """El anuncio que promocionó la publicación (None = publicación orgánica o no encontrada)."""
    row = await _local_ad_for_post(session, org, post_id)
    if row:
        return row
    conn = await connection(session, org, "meta")
    account, token = ad_account_id(conn), await meta_token(session, org)
    if not (account and token):
        return None
    try:  # consulta directa filtrando por la publicación
        page = await meta_get(f"act_{account}/ads", token, {
            "fields": f"id,{AD_FIELDS}", "limit": 50,
            "filtering": json.dumps([{"field": "effective_object_story_id", "operator": "EQUAL", "value": post_id}])})
        for ad in page.get("data") or []:
            if ad.get("id"):
                await _upsert_meta_ad(session, org, ad)
        await session.flush()
    except EnrichError:  # el filtro no es aceptado por la cuenta: se sincroniza el catálogo de anuncios
        await sync_meta_ads(session, org)
    return await _local_ad_for_post(session, org, post_id)


async def _update_touch(session: AsyncSession, attr: Attribution) -> None:
    """El último toque de la conversación con este origen recibe los nombres enriquecidos."""
    q = select(AttributionTouch).where(AttributionTouch.conversation_id == attr.conversation_id)
    if attr.post_id:
        q = q.where(AttributionTouch.post_id == attr.post_id)
    elif attr.ad_id:
        q = q.where(AttributionTouch.ad_id == attr.ad_id)
    elif attr.gclid:
        q = q.where(AttributionTouch.gclid == attr.gclid)
    else:
        return
    touch = (await session.scalars(q.order_by(AttributionTouch.occurred_at.desc()).limit(1))).first()
    if touch is not None:
        touch.ad_id = touch.ad_id or attr.ad_id
        touch.campaign_id, touch.campaign_name = attr.platform_campaign_id, attr.platform_campaign_name
        touch.ad_name = attr.ad_name
        touch.ad_headline = touch.ad_headline or attr.ad_headline


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
        if attr.ad_id or (attr.post_id and not attr.post_id.startswith("ig:")):
            if attr.ad_id:
                ad = await _meta_ad(session, attr.organization_id, attr.ad_id)
            else:
                ad = await meta_ad_for_post(session, attr.organization_id, attr.post_id)
            if ad is not None:
                attr.ad_id = ad.external_id
                attr.ad_name, attr.platform_campaign_id, attr.platform_campaign_name = (
                    ad.name, ad.campaign_id, ad.campaign_name)
                attr.ad_group_id, attr.ad_group_name = ad.ad_group_id, ad.ad_group_name
                attr.post_id = attr.post_id or ad.effective_object_story_id
                attr.ad_headline = attr.ad_headline or ad.headline
                attr.ad_body = attr.ad_body or ad.body
                attr.ad_media_type = attr.ad_media_type or ad.media_type
                attr.ad_media_url = attr.ad_media_url or ad.media_url
                attr.ad_thumbnail_url = attr.ad_thumbnail_url or ad.thumbnail_url
            await _update_touch(session, attr)  # publicación orgánica: queda "Publicación"
        elif attr.gclid:
            click = await _google_click(session, attr)
            attr.platform_campaign_id, attr.platform_campaign_name = click.campaign_id, click.campaign_name
            attr.ad_group_id, attr.ad_group_name, attr.keyword = click.ad_group_id, click.ad_group_name, click.keyword
            await _update_touch(session, attr)
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


async def sync_all_ad_catalogs() -> int:
    """Anuncios de Meta de todas las empresas con cuenta publicitaria configurada."""
    async with SessionLocal() as session:
        conns = (await session.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider == "meta", IntegrationConnection.status == "connected"))).all()
        orgs = [c.organization_id for c in conns if ad_account_id(c)]
    total = 0
    for org in orgs:
        async with SessionLocal() as session:
            try:
                total += await sync_meta_ads(session, org)
            except Exception as e:  # una cuenta con error no detiene a las demás
                await session.rollback()
                log.warning("No se pudo sincronizar el catálogo de anuncios de la empresa %s: %s", org, e)
    return total


async def ads_catalog_loop() -> None:
    """Registrar en main.py (LOOPS): anuncios de Meta cada 6 h (cruce publicación → anuncio sin llamar a la API)."""
    while True:
        try:
            n = await sync_all_ad_catalogs()
            if n:
                log.info("Catálogo de anuncios: %s anuncios sincronizados", n)
        except Exception:
            log.exception("Falló la sincronización del catálogo de anuncios")
        await asyncio.sleep(CATALOG_EVERY_S)
