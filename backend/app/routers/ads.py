"""Rendimiento por anuncio: conversaciones, clientes nuevos, ventas y conversiones contra la inversión (§16).

Lee reporting.daily_ads (recalculado para hoy/ayer al consultar, como el resto de reportes) y completa el
creativo desde ad_entities. Las divisiones por cero devuelven null.
"""

import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import AdEntity, Agent, Attribution, Contact, Conversation
from app.plans import feature_required
from app.routers.reports import _days, _range, _rows

router = APIRouter(prefix="/api", tags=["ads"], dependencies=[Depends(feature_required("attribution"))])
SUMS = ("spend", "impressions", "clicks", "conversations", "new_contacts", "sales", "conversions", "conversion_value",
        "deals_won_value")
_tasks: set[asyncio.Task] = set()


def _div(a: float, b: float, digits: int = 2) -> float | None:
    return round(a / b, digits) if b else None


def metrics(row: dict) -> dict:
    """Costos y retorno derivados de las sumas (null cuando el denominador es 0)."""
    spend = float(row.get("spend") or 0)
    value = float(row.get("conversion_value") or 0) + float(row.get("deals_won_value") or 0)
    return {
        "ctr": _div(100 * float(row.get("clicks") or 0), float(row.get("impressions") or 0)),
        "cost_per_conversation": _div(spend, row.get("conversations") or 0),
        "cost_per_new_contact": _div(spend, row.get("new_contacts") or 0),
        "cost_per_sale": _div(spend, row.get("sales") or 0),
        "roas": _div(value, spend),
    }


def _sum_into(acc: dict, r: dict) -> None:
    for k in SUMS:
        acc[k] = acc.get(k, 0) + (float(r[k]) if k in ("spend", "conversion_value", "deals_won_value") else int(r[k]))


@router.get("/reports/ads")
async def ads_report(start: date | None = None, end: date | None = None, platform: str | None = None,
                     campaign: str | None = None, agent: Agent = Depends(current_agent),
                     session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select d.day, d.platform, d.ad_key, d.ad_id, d.ad_name, d.campaign_id, d.campaign_name,
               d.conversations, d.new_contacts, d.sales, d.conversions, d.conversion_value::float as conversion_value,
               d.deals_won_value::float as deals_won_value, d.impressions, d.clicks, d.spend::float as spend
        from reporting.daily_ads d
        where d.organization_id = :o and d.day between :a and :b
          and (cast(:p as text) is null or d.platform = :p)
          and (cast(:c as text) is null or d.campaign_name ilike '%' || :c || '%' or d.campaign_id = :c)""",
                       o=org, a=start, b=end, p=platform, c=campaign)
    ads: dict[str, dict] = {}
    camps: dict[tuple, dict] = {}
    series: dict[str, dict] = {d.isoformat(): {"day": d.isoformat(), "spend": 0.0, "conversations": 0, "sales": 0}
                               for d in _days(start, end)}
    totals: dict = {}
    for r in rows:
        a = ads.setdefault(r["ad_key"], {"ad_key": r["ad_key"], "platform": r["platform"], "ad_id": r["ad_id"],
                                         "ad_name": r["ad_name"], "campaign_id": r["campaign_id"],
                                         "campaign_name": r["campaign_name"]})
        for k in ("ad_id", "ad_name", "campaign_id", "campaign_name"):
            a[k] = a[k] or r[k]
        _sum_into(a, r)
        ck = (r["platform"], r["campaign_id"] or r["campaign_name"] or "(sin campaña)")
        c = camps.setdefault(ck, {"platform": r["platform"], "campaign_id": r["campaign_id"],
                                  "campaign_name": r["campaign_name"] or "(sin campaña)"})
        _sum_into(c, r)
        _sum_into(totals, r)
        s = series.get(r["day"].isoformat())
        if s is not None:
            s["spend"] += float(r["spend"])
            s["conversations"] += r["conversations"]
            s["sales"] += r["sales"]

    # Creativo y estado desde la caché de anuncios
    ad_ids = [a["ad_id"] for a in ads.values() if a["ad_id"]]
    entities = {e.external_id: e for e in (await session.scalars(select(AdEntity).where(
        AdEntity.organization_id == org, AdEntity.entity_type == "ad", AdEntity.external_id.in_(ad_ids)))).all()
    } if ad_ids else {}
    out_ads = []
    for a in ads.values():
        e = entities.get(a["ad_id"] or "")
        a["ad_name"] = a["ad_name"] or (e.name if e else None) or _key_label(a["ad_key"])
        a["campaign_name"] = a["campaign_name"] or (e.campaign_name if e else None)
        a.update({"thumbnail_url": e.thumbnail_url if e else None, "headline": e.headline if e else None,
                  "status": e.status if e else None})
        a["spend"] = round(a.get("spend", 0), 2)
        out_ads.append({**a, **metrics(a)})
    out_ads.sort(key=lambda x: (-x.get("spend", 0), -x.get("conversations", 0)))
    out_camps = [{**c, "spend": round(c.get("spend", 0), 2), **metrics(c)} for c in camps.values()]
    out_camps.sort(key=lambda x: (-x.get("spend", 0), -x.get("conversations", 0)))
    totals = {k: totals.get(k, 0) for k in SUMS}
    totals["spend"] = round(totals["spend"], 2)
    cur = await _rows(session, """select currency from public.ad_spend_daily
                                   where organization_id = :o and currency is not null
                                   order by day desc limit 1""", o=org)
    currency = cur[0]["currency"] if cur else None
    return {"start": start, "end": end,
            "totals": {**totals, **{k: v for k, v in metrics(totals).items() if k != "ctr"}, "currency": currency},
            "ads": out_ads, "campaigns": out_camps, "series": [
                {**s, "spend": round(s["spend"], 2)} for s in series.values()]}


def _key_label(key: str) -> str:
    if key.startswith("post:"):
        return "Publicación"
    if key.startswith("campaign:"):
        return "Campaña (sin detalle por anuncio)"
    if key.startswith("utm:"):
        return key[4:]
    if key.startswith("channel:"):
        return key[8:]
    return key


@router.get("/ads/{platform}/{ad_id}")
async def ad_detail(platform: str, ad_id: str, start: date | None = None, end: date | None = None,
                    agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Creativo, serie diaria (start/end opcionales; 30 días por defecto) y clientes recientes del anuncio."""
    if platform not in ("meta", "google_ads"):
        raise HTTPException(404, "Plataforma no válida")
    org = agent.organization_id
    if start is None:  # por defecto, los últimos 30 días (el reporte general usa 7)
        end = end or date.today()
        start = end - timedelta(days=29)
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    e = await session.scalar(select(AdEntity).where(
        AdEntity.organization_id == org, AdEntity.platform == platform, AdEntity.entity_type == "ad",
        AdEntity.external_id == ad_id))
    rows = await _rows(session, """
        select day, sum(spend)::float as spend, sum(impressions)::bigint as impressions, sum(clicks)::bigint as clicks,
               sum(conversations)::int as conversations, sum(new_contacts)::int as new_contacts,
               sum(sales)::int as sales, sum(conversions)::int as conversions,
               sum(conversion_value)::float as conversion_value, sum(deals_won_value)::float as deals_won_value
        from reporting.daily_ads where organization_id = :o and platform = :p and ad_key = :k
          and day between :a and :b group by day order by day""", o=org, p=platform, k=ad_id, a=start, b=end)
    totals: dict = {}
    for r in rows:
        _sum_into(totals, r)
    totals = {k: totals.get(k, 0) for k in SUMS}
    if not e and not rows:
        attr = await session.scalar(select(Attribution.id).where(
            Attribution.organization_id == org, Attribution.ad_id == ad_id).limit(1))
        if attr is None:
            raise HTTPException(404, "Anuncio no encontrado")
    contacts = (await session.execute(
        select(Contact.id, Contact.name, Contact.first_source_at, Conversation.status)
        .join(Attribution, Attribution.contact_id == Contact.id)
        .join(Conversation, Conversation.id == Attribution.conversation_id)
        .where(Attribution.organization_id == org, Attribution.ad_id == ad_id)
        .order_by(Attribution.created_at.desc()).limit(50))).all()
    seen, recent = set(), []
    for cid, name, first_at, status in contacts:
        if cid in seen:
            continue
        seen.add(cid)
        recent.append({"contact_id": cid, "name": name, "first_source_at": first_at, "status": status})
    creative = None
    if e:
        creative = {"name": e.name, "status": e.status, "campaign_id": e.campaign_id, "campaign_name": e.campaign_name,
                    "ad_group_id": e.ad_group_id, "ad_group_name": e.ad_group_name, "headline": e.headline,
                    "body": e.body, "media_type": e.media_type, "media_url": e.media_url,
                    "thumbnail_url": e.thumbnail_url, "destination": e.destination,
                    "post_id": e.effective_object_story_id, "fetched_at": e.fetched_at}
    return {"platform": platform, "ad_id": ad_id, "creative": creative,
            "totals": {**totals, **metrics(totals)}, "series": rows, "contacts": recent[:20]}


@router.post("/ads/sync")
async def ads_sync(agent: Agent = Depends(require_admin)):
    """Sincroniza ya la inversión y el catálogo de anuncios de la empresa (en segundo plano)."""
    from app.ad_enrichment import sync_meta_ads
    from app.ads.spend import sync_org
    from app.db import SessionLocal

    org = agent.organization_id

    async def job():
        async with SessionLocal() as session:
            try:
                await sync_meta_ads(session, org)
            except Exception:  # noqa: BLE001 — sin cuenta configurada o error de Meta: la inversión sigue
                await session.rollback()
        await sync_org(org)

    task = asyncio.get_running_loop().create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"started": True}
