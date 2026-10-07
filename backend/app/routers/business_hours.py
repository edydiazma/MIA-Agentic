"""Horarios de atención: general, por grupo y festivos (§18.1)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import business_hours as bh
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, BusinessHoliday, BusinessHours, Group, Organization, utcnow

router = APIRouter(prefix="/api/business-hours", tags=["business-hours"])


class HoursIn(BaseModel):
    timezone: str | None = None
    schedule: dict = {}
    out_of_hours_message: str | None = Field(default=None, max_length=4000)
    assign_anyway: bool = False
    pause_bot: bool = False
    inherit_general: bool = False


class HolidayIn(BaseModel):
    day: date
    name: str = Field(min_length=1, max_length=120)
    group_id: int | None = None


async def _group(session: AsyncSession, org: int, gid: int) -> Group:
    g = await session.get(Group, gid)
    if not g or g.organization_id != org:
        raise HTTPException(404, "Grupo no encontrado")
    return g


def _check(body: HoursIn) -> dict:
    try:
        schedule = bh.validate_schedule(body.schedule)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Zona horaria inválida") from None
    return schedule


@router.get("")
async def get_hours(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    general = await bh.ensure_general(session, org)
    await session.commit()
    groups = (await session.scalars(select(Group).where(Group.organization_id == org).order_by(Group.name))).all()
    rows = {r.group_id: r for r in (await session.scalars(select(BusinessHours).where(
        BusinessHours.organization_id == org, BusinessHours.group_id.is_not(None)))).all()}
    holidays = (await session.scalars(select(BusinessHoliday).where(BusinessHoliday.organization_id == org)
                                      .order_by(BusinessHoliday.day))).all()
    now_general = await bh.status(session, org, None)
    out_groups = []
    for g in groups:
        st = await bh.status(session, org, g.id)
        row = rows.get(g.id)
        out_groups.append({"group_id": g.id, "group_name": g.name, "hours": bh.row_out(row),
                           "uses_general": row is None or row.inherit_general, "open_now": st["open"]})
    tz = (await session.get(Organization, org)).timezone
    return {"general": bh.row_out(general), "default_timezone": tz, "open_now": now_general["open"],
            "configured": now_general["configured"], "groups": out_groups,
            "holidays": [{"id": h.id, "day": h.day, "name": h.name, "group_id": h.group_id} for h in holidays]}


@router.put("/general")
async def put_general(body: HoursIn, admin: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    schedule = _check(body)
    row = await bh.ensure_general(session, org)
    if row is None:
        row = BusinessHours(organization_id=org, group_id=None)
        session.add(row)
    row.timezone = body.timezone or (await session.get(Organization, org)).timezone
    row.schedule, row.out_of_hours_message = schedule, body.out_of_hours_message
    row.assign_anyway, row.pause_bot, row.inherit_general = body.assign_anyway, body.pause_bot, False
    row.updated_at = utcnow()
    # Grupos que heredan el general: se copia (quedan iguales y visibles en su ficha)
    for g in (await session.scalars(select(BusinessHours).where(
            BusinessHours.organization_id == org, BusinessHours.group_id.is_not(None), BusinessHours.inherit_general))).all():
        g.timezone, g.schedule, g.out_of_hours_message = row.timezone, schedule, body.out_of_hours_message
        g.assign_anyway, g.pause_bot, g.updated_at = body.assign_anyway, body.pause_bot, utcnow()
    await session.commit()
    return bh.row_out(row)


@router.put("/groups/{gid}")
async def put_group(gid: int, body: HoursIn, admin: Agent = Depends(require_admin),
                    session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    await _group(session, org, gid)
    schedule = _check(body)
    row = await session.scalar(select(BusinessHours).where(BusinessHours.organization_id == org,
                                                           BusinessHours.group_id == gid))
    if row is None:
        row = BusinessHours(organization_id=org, group_id=gid)
        session.add(row)
    if body.inherit_general:
        general = await bh.ensure_general(session, org)
        if general is None:
            raise HTTPException(422, "Primero configura el horario general")
        row.timezone, row.schedule, row.out_of_hours_message = general.timezone, general.schedule, general.out_of_hours_message
        row.assign_anyway, row.pause_bot = general.assign_anyway, general.pause_bot
    else:
        row.timezone = body.timezone or (await session.get(Organization, org)).timezone
        row.schedule, row.out_of_hours_message = schedule, body.out_of_hours_message
        row.assign_anyway, row.pause_bot = body.assign_anyway, body.pause_bot
    row.inherit_general, row.updated_at = body.inherit_general, utcnow()
    await session.commit()
    return bh.row_out(row)


@router.delete("/groups/{gid}")
async def delete_group_hours(gid: int, admin: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    """El grupo vuelve a usar el horario general."""
    row = await session.scalar(select(BusinessHours).where(BusinessHours.organization_id == admin.organization_id,
                                                           BusinessHours.group_id == gid))
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"ok": True}


@router.post("/holidays")
async def add_holiday(body: HolidayIn, admin: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    if body.group_id:
        await _group(session, org, body.group_id)
    exists = await session.scalar(select(BusinessHoliday.id).where(
        BusinessHoliday.organization_id == org, BusinessHoliday.day == body.day,
        BusinessHoliday.group_id.is_(None) if body.group_id is None else BusinessHoliday.group_id == body.group_id))
    if exists:
        raise HTTPException(409, "Ese festivo ya existe")
    h = BusinessHoliday(organization_id=org, day=body.day, name=body.name.strip(), group_id=body.group_id)
    session.add(h)
    await session.commit()
    return {"id": h.id, "day": h.day, "name": h.name, "group_id": h.group_id}


@router.delete("/holidays/{hid}")
async def delete_holiday(hid: int, admin: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    h = await session.get(BusinessHoliday, hid)
    if not h or h.organization_id != admin.organization_id:
        raise HTTPException(404, "Festivo no encontrado")
    await session.delete(h)
    await session.commit()
    return {"ok": True}


@router.get("/status")
async def hours_status(group_id: int | None = None, agent: Agent = Depends(current_agent),
                       session: AsyncSession = Depends(get_session)):
    st = await bh.status(session, agent.organization_id, group_id)
    await session.commit()
    row = st["row"]
    return {"configured": st["configured"], "open": st["open"], "group_id": group_id,
            "uses": None if row is None else ("group" if row.group_id else "general"),
            "closed_since": st.get("closed_since")}
