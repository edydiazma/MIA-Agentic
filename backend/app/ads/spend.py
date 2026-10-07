"""Sincroniza la inversión por anuncio y día a ad_spend_daily.

- Meta: Insights de la cuenta publicitaria (level=ad, time_increment=1): spend, impressions, clicks,
  account_currency y conversaciones iniciadas (actions onsite_conversion.messaging_conversation_started_*).
  Los días son los de la zona horaria de la cuenta.
- Google Ads: GAQL sobre ad_group_ad (costo por anuncio) y sobre campaign (campañas sin filas por anuncio, p. ej.
  Performance Max). cost_micros / 1e6.
Cada hora se re-sincronizan los últimos 3 días (las plataformas ajustan cifras); la primera vez, 30 días.
Idempotente (upsert por organización, plataforma, nivel, entidad y día). Un error queda en la conexión y en una
alerta; nunca detiene el ciclo.
"""

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ad_enrichment import EnrichError, ad_account_id, connection, google_search, meta_get, meta_token
from app.db import SessionLocal
from app.models import AdSpendDaily, IntegrationConnection, utcnow

log = logging.getLogger(__name__)
RECENT_DAYS = 3
BACKFILL_DAYS = 30
SYNC_EVERY_S = 3600
MESSAGING_ACTION = "onsite_conversion.messaging_conversation_started"
UPDATABLE = ("account_id", "campaign_id", "campaign_name", "ad_group_id", "ad_group_name", "ad_id", "ad_name",
             "impressions", "clicks", "spend", "currency", "platform_conversations", "fetched_at")


class SpendError(Exception):
    pass


async def _upsert(session: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = insert(AdSpendDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["organization_id", "platform", "level", "entity_id", "day"],
        set_={k: getattr(stmt.excluded, k) for k in UPDATABLE})
    await session.execute(stmt)
    return len(rows)


async def _since(session: AsyncSession, org: int, platform: str) -> date:
    has = await session.scalar(select(func.count()).where(
        AdSpendDaily.organization_id == org, AdSpendDaily.platform == platform))
    return date.today() - timedelta(days=(RECENT_DAYS if has else BACKFILL_DAYS) - 1)


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _messaging(actions: list | None) -> int | None:
    found = [_int(a.get("value")) for a in actions or [] if str(a.get("action_type", "")).startswith(MESSAGING_ACTION)]
    return max(found) if found else None  # varias ventanas (7d, 1d…): se toma la mayor, sin sumar duplicados


async def sync_meta(session: AsyncSession, org: int, since: date | None = None, until: date | None = None) -> int:
    conn = await connection(session, org, "meta")
    account = ad_account_id(conn)
    if not account:
        raise SpendError("Configura la cuenta publicitaria de Meta (act_…) en Conversiones")
    token = await meta_token(session, org)
    if not token:
        raise SpendError("Falta el token de Meta (META_CAPI_TOKEN o conexión Meta)")
    since = since or await _since(session, org, "meta")
    until = until or date.today()
    path = f"act_{account}/insights"
    params = {"level": "ad", "time_increment": 1, "limit": 500,
              "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,impressions,clicks,"
                        "actions,account_currency,date_start",
              "time_range": f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}'}
    total = 0
    for _ in range(100):
        try:
            page = await meta_get(path, token, params)
        except EnrichError as e:
            raise SpendError(str(e)) from e
        rows = [{
            "organization_id": org, "platform": "meta", "level": "ad", "entity_id": str(r["ad_id"]),
            "day": date.fromisoformat(r["date_start"]), "account_id": account,
            "campaign_id": r.get("campaign_id"), "campaign_name": r.get("campaign_name"),
            "ad_group_id": r.get("adset_id"), "ad_group_name": r.get("adset_name"),
            "ad_id": str(r["ad_id"]), "ad_name": r.get("ad_name"),
            "impressions": _int(r.get("impressions")), "clicks": _int(r.get("clicks")),
            "spend": round(float(r.get("spend") or 0), 2), "currency": (r.get("account_currency") or None),
            "platform_conversations": _messaging(r.get("actions")), "fetched_at": utcnow(),
        } for r in page.get("data") or [] if r.get("ad_id") and r.get("date_start")]
        total += await _upsert(session, rows)
        nxt = (page.get("paging") or {}).get("next")
        if not nxt:
            break
        path, params = nxt, {}
    await session.commit()
    return total


async def sync_google(session: AsyncSession, org: int, since: date | None = None, until: date | None = None) -> int:
    conn = await connection(session, org, "google_ads")
    if not conn or conn.status != "connected":
        raise SpendError("Google Ads no está conectado")
    since = since or await _since(session, org, "google_ads")
    until = until or date.today()
    span = f"segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
    account = (conn.external_account_id or "").replace("-", "") or None
    try:
        ads = await google_search(session, conn, (
            "SELECT segments.date, campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
            "ad_group_ad.ad.name, metrics.cost_micros, metrics.impressions, metrics.clicks, customer.currency_code "
            f"FROM ad_group_ad WHERE {span}"))
        campaigns = await google_search(session, conn, (
            "SELECT segments.date, campaign.id, campaign.name, metrics.cost_micros, metrics.impressions, "
            f"metrics.clicks, customer.currency_code FROM campaign WHERE {span}"))
    except EnrichError as e:
        raise SpendError(str(e)) from e

    def base(r: dict) -> dict:
        m, c = r.get("metrics") or {}, r.get("campaign") or {}
        return {"organization_id": org, "platform": "google_ads", "day": date.fromisoformat(r["segments"]["date"]),
                "account_id": account, "campaign_id": str(c["id"]) if c.get("id") else None,
                "campaign_name": c.get("name"), "impressions": _int(m.get("impressions")),
                "clicks": _int(m.get("clicks")), "spend": round(_int(m.get("costMicros")) / 1_000_000, 2),
                "currency": (r.get("customer") or {}).get("currencyCode"), "platform_conversations": None,
                "fetched_at": utcnow()}

    rows: dict[tuple, dict] = {}
    for r in ads:
        ad = ((r.get("adGroupAd") or {}).get("ad") or {})
        if not ad.get("id") or not (r.get("segments") or {}).get("date"):
            continue
        g = r.get("adGroup") or {}
        row = {**base(r), "level": "ad", "entity_id": str(ad["id"]), "ad_id": str(ad["id"]), "ad_name": ad.get("name"),
               "ad_group_id": str(g["id"]) if g.get("id") else None, "ad_group_name": g.get("name")}
        rows[("ad", row["entity_id"], row["day"])] = row
    for r in campaigns:
        c = r.get("campaign") or {}
        if not c.get("id") or not (r.get("segments") or {}).get("date"):
            continue
        row = {**base(r), "level": "campaign", "entity_id": str(c["id"]), "ad_id": None, "ad_name": None,
               "ad_group_id": None, "ad_group_name": None}
        rows[("campaign", row["entity_id"], row["day"])] = row
    total = await _upsert(session, list(rows.values()))
    await session.commit()
    return total


async def _record_error(org: int, provider: str, message: str) -> None:
    from app.service import create_alert

    async with SessionLocal() as session:
        conn = await connection(session, org, provider)
        if conn is not None:
            already = conn.last_error == message
            conn.last_error = message[:1000]
            await session.commit()
            if already:
                return  # no repetir la misma alerta cada hora
        await create_alert(session, org, severity="warning", layer="integration", source="system",
                           title="No se pudo sincronizar la inversión publicitaria",
                           description=f"{'Meta' if provider == 'meta' else 'Google Ads'}: {message}"[:1000],
                           ref=f"ad_spend:{provider}")


async def sync_org(org: int) -> dict:
    """Sincroniza ambas plataformas de una empresa; devuelve filas por plataforma o el error."""
    out: dict = {}
    for provider, fn in (("meta", sync_meta), ("google_ads", sync_google)):
        async with SessionLocal() as session:
            conn = await connection(session, org, provider)
            if not conn or conn.status != "connected" or (provider == "meta" and not ad_account_id(conn)):
                continue
            try:
                out[provider] = await fn(session, org)
                if conn.last_error:
                    conn.last_error = None
                    await session.commit()
            except Exception as e:  # noqa: BLE001 — nunca detiene el ciclo
                await session.rollback()
                out[provider] = f"error: {e}"
                log.warning("Inversión %s de la empresa %s: %s", provider, org, e)
                await _record_error(org, provider, str(e))
    return out


async def sync_all() -> int:
    async with SessionLocal() as session:
        orgs = sorted(set((await session.scalars(select(IntegrationConnection.organization_id).where(
            IntegrationConnection.provider.in_(["meta", "google_ads"]),
            IntegrationConnection.status == "connected"))).all()))
    for org in orgs:
        await sync_org(org)
    return len(orgs)


async def spend_loop() -> None:
    """Registrar en main.py (LOOPS): inversión de Meta y Google Ads cada hora."""
    while True:
        try:
            await sync_all()
        except Exception:
            log.exception("Falló la sincronización de inversión publicitaria")
        await asyncio.sleep(SYNC_EVERY_S)
