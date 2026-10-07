"""BI de clientes (primera/última interacción, recencia, antigüedad) y demanda de productos. docs/data-model.md §14"""

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.db import get_session
from app.models import Agent
from app.routers.reports import _days, _pct, _range, _rows

router = APIRouter(prefix="/api/reports", tags=["reports"])

RECENCY = [("0-1 días", 0, 2), ("2-7 días", 2, 8), ("8-30 días", 8, 31), ("31-90 días", 31, 91),
           ("Más de 90 días", 91, None)]
LIFETIME = [("Menos de 1 día", 0, 1), ("1-7 días", 1, 7), ("8-30 días", 7, 30), ("31-90 días", 30, 90),
            ("91-365 días", 90, 365), ("Más de 1 año", 365, None)]


def _bucket_sql(column: str, buckets: list[tuple]) -> str:
    parts = []
    for label, lo, hi in buckets:
        cond = f"{column} >= {lo}" if hi is None else f"{column} >= {lo} and {column} < {hi}"
        if lo == 0:
            cond = f"{column} < {hi}"
        parts.append(f"count(*) filter (where {cond}) as \"{label}\"")
    return ", ".join(parts)


@router.get("/customers")
async def customers_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    """Clientes nuevos, activos y recurrentes del periodo; recencia, antigüedad y distribución por canal y asesor."""
    org = agent.organization_id
    lo, hi, tz, start, end = await _range(session, org, start, end)
    tzname = str(tz)
    p = {"o": org, "lo": lo, "hi": hi, "tz": tzname}

    totals = (await _rows(session, """
        select count(*) as contacts,
               count(*) filter (where b.created_at >= :lo and b.created_at < :hi) as new,
               round(avg(b.lifetime_days) filter (where b.lifetime_days is not null), 2) as avg_lifetime_days,
               round(avg(b.avg_days_between_conversations), 2) as avg_days_between_conversations,
               round(percentile_cont(0.5) within group (order by b.days_since_last_interaction)::numeric, 2)
                 as median_days_since_last_interaction
        from reporting.v_contact_bi b
        join public.contacts k on k.id = b.contact_id and not k.blocked
        where b.organization_id = :o""", **p))[0]

    # Activos: clientes que escribieron en el periodo (mensajes entrantes, particionado por fecha)
    daily = await _rows(session, """
        select (m.created_at at time zone :tz)::date as day, count(distinct c.contact_id) as active
        from public.messages m join public.conversations c on c.id = m.conversation_id
        where m.organization_id = :o and m.direction = 'in' and m.created_at >= :lo and m.created_at < :hi
        group by 1""", **p)
    active_row = (await _rows(session, """
        select count(distinct c.contact_id) as active,
               count(distinct c.contact_id) filter (where k.first_interaction_at < :lo) as returning
        from public.messages m join public.conversations c on c.id = m.conversation_id
        join public.contacts k on k.id = c.contact_id
        where m.organization_id = :o and m.direction = 'in' and m.created_at >= :lo and m.created_at < :hi""", **p))[0]
    new_daily = await _rows(session, """
        select (created_at at time zone :tz)::date as day, count(*) as new
        from public.contacts where organization_id = :o and created_at >= :lo and created_at < :hi group by 1""", **p)

    recency = (await _rows(session, f"""
        select {_bucket_sql("days_since_last_interaction", RECENCY)},
               count(*) filter (where b.last_interaction_at is null) as "Sin interacción"
        from reporting.v_contact_bi b join public.contacts k on k.id = b.contact_id and not k.blocked
        where b.organization_id = :o""", o=org))[0]
    lifetime = (await _rows(session, f"""
        select {_bucket_sql("lifetime_days", LIFETIME)}
        from reporting.v_contact_bi b join public.contacts k on k.id = b.contact_id and not k.blocked
        where b.organization_id = :o and b.lifetime_days is not null""", o=org))[0]
    by_channel = await _rows(session, """
        select p.provider, count(*) as contacts,
               count(*) filter (where k.created_at >= :lo and k.created_at < :hi) as new
        from public.contacts k cross join lateral unnest(k.channel_providers) as p(provider)
        where k.organization_id = :o and not k.blocked
        group by 1 order by 2 desc""", **p)
    by_agent = await _rows(session, """
        select k.last_agent_id as agent_id, a.name, count(*) as contacts
        from public.contacts k join public.agents a on a.id = k.last_agent_id
        where k.organization_id = :o and not k.blocked
        group by 1, 2 order by 3 desc limit 50""", o=org)

    active_by_day = {r["day"]: r["active"] for r in daily}
    new_by_day = {r["day"]: r["new"] for r in new_daily}
    return {
        "start": start, "end": end,
        "totals": {"contacts": totals["contacts"], "new": totals["new"], "active": active_row["active"],
                   "returning": active_row["returning"],
                   "avg_lifetime_days": float(totals["avg_lifetime_days"]) if totals["avg_lifetime_days"] is not None
                   else None,
                   "avg_days_between_conversations": float(totals["avg_days_between_conversations"])
                   if totals["avg_days_between_conversations"] is not None else None,
                   "median_days_since_last_interaction": float(totals["median_days_since_last_interaction"])
                   if totals["median_days_since_last_interaction"] is not None else None},
        "new_series": [{"day": d.isoformat(), "new": new_by_day.get(d, 0), "active": active_by_day.get(d, 0)}
                       for d in _days(start, end)],
        "recency_buckets": [{"label": k, "count": v} for k, v in recency.items()],
        "lifetime_buckets": [{"label": k, "count": v} for k, v in lifetime.items()],
        "by_channel": by_channel,
        "by_agent": by_agent,
    }


@router.get("/products")
async def products_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    """Demanda por producto: menciones, interés, cotizaciones, compras y valor (reporting.daily_products)."""
    org = agent.organization_id
    lo, hi, _tz, start, end = await _range(session, org, start, end)
    p = {"o": org, "a": start, "b": end}
    top = await _rows(session, """
        select product_key, max(product_id) as product_id, max(name) as name, sum(mentioned)::int as mentioned,
               sum(interested)::int as interested, sum(quoted)::int as quoted, sum(purchased)::int as purchased,
               sum(purchased_value)::float as purchased_value, sum(contacts)::int as contacts
        from reporting.daily_products where organization_id = :o and day between :a and :b
        group by product_key order by sum(interested + quoted + purchased + mentioned) desc limit 50""", **p)
    for r in top:
        r["conversion_pct"] = _pct(r["purchased"], r["interested"] + r["quoted"] + r["purchased"])
    series = {r["day"]: r for r in await _rows(session, """
        select day, sum(interested)::int as interested, sum(quoted)::int as quoted, sum(purchased)::int as purchased
        from reporting.daily_products where organization_id = :o and day between :a and :b group by day""", **p)}
    totals = (await _rows(session, """
        select coalesce(sum(mentioned), 0)::int as mentioned, coalesce(sum(interested), 0)::int as interested,
               coalesce(sum(quoted), 0)::int as quoted, coalesce(sum(purchased), 0)::int as purchased,
               coalesce(sum(purchased_value), 0)::float as purchased_value
        from reporting.daily_products where organization_id = :o and day between :a and :b""", **p))[0]
    totals["contacts"] = (await _rows(session, """
        select count(distinct contact_id) as n from public.interaction_products
        where organization_id = :o and created_at >= :lo and created_at < :hi""", o=org, lo=lo, hi=hi))[0]["n"]
    return {
        "start": start, "end": end, "totals": totals, "top": top,
        "series": [{"day": d.isoformat(), "interested": series.get(d, {}).get("interested", 0),
                    "quoted": series.get(d, {}).get("quoted", 0), "purchased": series.get(d, {}).get("purchased", 0)}
                   for d in _days(start, end)],
    }
