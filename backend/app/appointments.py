"""Disponibilidad y reserva de citas (las usan el bot, los flujos y la UI)."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Organization, utcnow
from app.settings_store import get_setting


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def config(session: AsyncSession, org: int) -> tuple[dict, ZoneInfo]:
    cfg = await get_setting(session, "appointments", org)
    tz = ZoneInfo((await session.get(Organization, org)).timezone)
    return cfg, tz


async def available_slots(session: AsyncSession, org: int, day: date) -> list[datetime]:
    cfg, tz = await config(session, org)
    now = utcnow()
    if day.weekday() not in cfg["days"] or day > (now.astimezone(tz).date() + timedelta(days=cfg["max_days_ahead"])):
        return []
    h1, m1 = map(int, cfg["start"].split(":"))
    h2, m2 = map(int, cfg["end"].split(":"))
    cur = datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz)
    end = datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz)
    step = timedelta(minutes=cfg["duration_min"])

    taken_rows = (await session.execute(
        select(Appointment.starts_at, func.count())
        .where(Appointment.organization_id == org, Appointment.status == "scheduled",
               Appointment.starts_at >= cur.astimezone(UTC), Appointment.starts_at < end.astimezone(UTC))
        .group_by(Appointment.starts_at))).all()
    taken = {_utc(s): n for s, n in taken_rows}

    slots = []
    while cur + step <= end:
        if cur > now and taken.get(cur.astimezone(UTC), 0) < cfg["capacity"]:
            slots.append(cur)
        cur += step
    return slots


def parse_local(day: str, hhmm: str, tz: ZoneInfo) -> datetime:
    d = date.fromisoformat(day)
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
