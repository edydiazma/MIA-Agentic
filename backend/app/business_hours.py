"""Horario de atención por grupo: varios rangos por día, festivos, zona horaria, mensaje fuera de horario,
asignar igual y pausar el bot. docs/data-model.md §18.1

El horario general es la fila con group_id null; un grupo sin fila (o con inherit_general) usa el general.
La regla anterior (automatización business_hours {days, start, end, message}) se migra al horario general la
primera vez que se consulta (idempotente).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Automation, BusinessHoliday, BusinessHours, Conversation, Message, Organization, utcnow

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Bogota")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Bogota")


def _hm(value: str) -> int:
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


def validate_schedule(schedule: dict) -> dict:
    """Normaliza {mon: [{from, to}], ...}; rangos HH:MM, from < to, sin solaparse. Lanza ValueError."""
    out: dict[str, list[dict]] = {}
    for key in DAY_KEYS:
        ranges = []
        for r in (schedule or {}).get(key) or []:
            f, t = str(r.get("from", "")), str(r.get("to", ""))
            try:
                a, b = _hm(f), _hm(t)
            except ValueError:
                raise ValueError(f"Hora inválida en {key}: {f}–{t}") from None
            if not (0 <= a < b <= 24 * 60):
                raise ValueError(f"Rango inválido en {key}: {f}–{t} (la hora inicial debe ser menor)")
            ranges.append({"from": f"{a // 60:02d}:{a % 60:02d}", "to": f"{b // 60:02d}:{b % 60:02d}"})
        ranges.sort(key=lambda r: r["from"])
        for prev, nxt in zip(ranges, ranges[1:], strict=False):
            if _hm(nxt["from"]) < _hm(prev["to"]):
                raise ValueError(f"Rangos superpuestos en {key}")
        out[key] = ranges
    return out


def legacy_to_schedule(cfg: dict) -> dict:
    days = cfg.get("days", [0, 1, 2, 3, 4])
    start, end = cfg.get("start", "08:00"), cfg.get("end", "18:00")
    if end in ("24:00", "23:59"):
        end = "24:00"
    return {k: ([{"from": start, "to": end}] if i in days else []) for i, k in enumerate(DAY_KEYS)}


async def ensure_general(session: AsyncSession, org: int) -> BusinessHours | None:
    """Horario general; si no existe y hay una regla business_hours anterior, la migra."""
    row = await session.scalar(select(BusinessHours).where(
        BusinessHours.organization_id == org, BusinessHours.group_id.is_(None)))
    if row is not None:
        return row
    rule = (await session.scalars(select(Automation).where(
        Automation.organization_id == org, Automation.type == "business_hours").order_by(Automation.id).limit(1))).first()
    if rule is None:
        return None
    cfg = rule.config or {}
    tz = cfg.get("timezone") or (await session.get(Organization, org)).timezone
    row = BusinessHours(organization_id=org, group_id=None, timezone=tz, schedule=legacy_to_schedule(cfg),
                        out_of_hours_message=cfg.get("message"), assign_anyway=True, pause_bot=False)
    session.add(row)
    await session.flush()
    return row


async def hours_for(session: AsyncSession, org: int, group_id: int | None) -> BusinessHours | None:
    general = await ensure_general(session, org)
    if group_id:
        row = await session.scalar(select(BusinessHours).where(
            BusinessHours.organization_id == org, BusinessHours.group_id == group_id))
        if row is not None and not row.inherit_general:
            return row
    return general


async def _holidays(session: AsyncSession, org: int, group_id: int | None, start: date, end: date) -> set[date]:
    stmt = select(BusinessHoliday.day).where(BusinessHoliday.organization_id == org, BusinessHoliday.day >= start,
                                             BusinessHoliday.day <= end)
    if group_id:
        stmt = stmt.where((BusinessHoliday.group_id.is_(None)) | (BusinessHoliday.group_id == group_id))
    else:
        stmt = stmt.where(BusinessHoliday.group_id.is_(None))
    return set((await session.scalars(stmt)).all())


def open_at(row: BusinessHours, when: datetime, holidays: set[date] = frozenset()) -> bool:
    local = when.astimezone(_tz(row.timezone))
    if local.date() in holidays:
        return False
    minute = local.hour * 60 + local.minute
    return any(_hm(r["from"]) <= minute < _hm(r["to"]) for r in (row.schedule or {}).get(DAY_KEYS[local.weekday()]) or [])


def last_close(row: BusinessHours, when: datetime, holidays: set[date] = frozenset()) -> datetime | None:
    """Último momento en que cerró (fin del rango anterior) — para enviar el aviso una vez por periodo."""
    tz = _tz(row.timezone)
    local = when.astimezone(tz)
    for back in range(0, 15):
        day = local.date() - timedelta(days=back)
        if day in holidays:
            continue
        ends = sorted((_hm(r["to"]) for r in (row.schedule or {}).get(DAY_KEYS[day.weekday()]) or []), reverse=True)
        for end in ends:
            moment = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=end)
            if moment <= local:
                return moment
    return None


async def status(session: AsyncSession, org: int, group_id: int | None, when: datetime | None = None) -> dict:
    """{configured, open, row}. Sin horario configurado: siempre abierto."""
    when = when or utcnow()
    row = await hours_for(session, org, group_id)
    if row is None:
        return {"configured": False, "open": True, "row": None}
    local_day = when.astimezone(_tz(row.timezone)).date()
    hol = await _holidays(session, org, group_id, local_day - timedelta(days=15), local_day)
    return {"configured": True, "open": open_at(row, when, hol), "row": row,
            "closed_since": None if open_at(row, when, hol) else last_close(row, when, hol)}


async def send_out_of_hours(session: AsyncSession, conv: Conversation, st: dict) -> bool:
    """Envía el mensaje fuera de horario una sola vez por conversación y periodo cerrado."""
    from app.service import send_text

    row = st.get("row")
    text = (row.out_of_hours_message or "").strip() if row else ""
    if not text:
        return False
    since = st.get("closed_since")
    stmt = select(Message.id).where(Message.conversation_id == conv.id, Message.direction == "out",
                                    Message.sender_type == "bot", Message.text == text)
    if since is not None:
        stmt = stmt.where(Message.created_at >= since)
    if await session.scalar(stmt.limit(1)):
        return False
    await send_text(session, conv, text, sender_type="bot")
    return True


def row_out(row: BusinessHours | None) -> dict | None:
    if row is None:
        return None
    return {"id": row.id, "group_id": row.group_id, "timezone": row.timezone,
            "schedule": validate_schedule(row.schedule or {}), "out_of_hours_message": row.out_of_hours_message,
            "assign_anyway": row.assign_anyway, "pause_bot": row.pause_bot, "inherit_general": row.inherit_general,
            "updated_at": row.updated_at}
