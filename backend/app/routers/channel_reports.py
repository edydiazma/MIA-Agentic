"""Reportes → Canales: conversaciones, mensajes y contactos nuevos por canal (reporting.daily_channels)."""

from collections import Counter, defaultdict
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.channels import LABELS
from app.db import get_session
from app.models import Agent, Channel
from app.routers.reports import _days, _range, _rows

router = APIRouter(prefix="/api", tags=["reports"])
KEYS = ("conversations", "inbound", "outbound", "contacts_new")


@router.get("/reports/channels")
async def channels_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    rows = await _rows(session, """
        select day, channel_id, provider, conversations, inbound, outbound, contacts_new
        from reporting.daily_channels where organization_id = :o and day between :a and :b""", o=org, a=start, b=end)
    channels = {c.id: c for c in (await session.scalars(
        select(Channel).where(Channel.organization_id == org))).unique().all()}
    by_channel: dict[int, Counter] = defaultdict(Counter)
    by_provider: dict[str, Counter] = defaultdict(Counter)
    series: dict[str, Counter] = {d.isoformat(): Counter() for d in _days(start, end)}
    for r in rows:
        for k in KEYS:
            by_channel[r["channel_id"]][k] += r[k]
            by_provider[r["provider"]][k] += r[k]
        series[r["day"].isoformat()][r["provider"]] += r["conversations"]
    totals = Counter()
    for v in by_channel.values():
        totals.update(v)
    return {
        "totals": {k: totals.get(k, 0) for k in KEYS},
        "by_provider": sorted([{"provider": p, "label": LABELS.get(p, p), **{k: v.get(k, 0) for k in KEYS}}
                               for p, v in by_provider.items()], key=lambda r: -r["conversations"]),
        "by_channel": sorted([{"channel_id": cid, "name": channels[cid].name if cid in channels else "(eliminado)",
                               "provider": channels[cid].provider if cid in channels else None,
                               **{k: v.get(k, 0) for k in KEYS}} for cid, v in by_channel.items()],
                             key=lambda r: -r["conversations"]),
        "series": [{"day": d, **{p: v.get(p, 0) for p in LABELS}} for d, v in series.items()],
    }
