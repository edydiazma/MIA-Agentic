"""Programador de journeys (una réplica líder; la ejecución va a la cola «outbound»).

Cada ciclo:
1. Inscripciones vencidas (`next_run_at <= now`) → trabajo `journeys.run` (o en línea si la cola está apagada).
2. Segmentos dinámicos vencidos según `refresh_minutes` → miembros nuevos entran a los journeys
   «entra al segmento» / «miembro del segmento».
3. Entradas por fecha (p. ej. vencimiento del SOAT − 30 días, a las 10:00 hora de la empresa), una vez al día.
4. Eventos (conversación cerrada, negocio ganado/perdido, cambio de etapa, cita agendada / no asistió, pedido
   pagado) leídos desde un cursor por empresa (`org_settings.journeys_state`) → entradas y metas.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.jobs import job
from app.journeys import engine
from app.models import Journey, JourneyEnrollment, OrgSetting, Segment, utcnow

log = logging.getLogger(__name__)
INTERVAL_S = 30
BATCH = 500
STATE_KEY = "journeys_state"
GOAL_OF_EVENT = {"deal_won": "deal_won", "appointment_booked": "appointment_booked", "order_paid": "purchase",
                 "stage_changed": "stage"}


@job("journeys.run", queue="outbound", max_attempts=3)
async def run_job(payload: dict) -> dict:
    at = payload.get("enrolled_at")
    status = await engine.run_enrollment(int(payload["enrollment_id"]), datetime.fromisoformat(at) if at else None)
    return {"status": status}


async def journey_loop() -> None:
    while True:
        await asyncio.sleep(INTERVAL_S)
        try:
            await tick()
        except Exception:
            log.exception("Falló el ciclo de journeys")


async def tick(now: datetime | None = None) -> dict:
    """Un ciclo completo (los tests lo llaman directamente)."""
    now = now or utcnow()
    out = {"segments": await refresh_due_segments(now), "dates": await run_date_entries(now),
           "events": await poll_events(now)}
    out["due"] = await dispatch_due(now)
    return out


# --- 1. Inscripciones vencidas -------------------------------------------------------------------------------
async def dispatch_due(now: datetime | None = None) -> int:
    from app import jobs

    now = now or utcnow()
    async with SessionLocal() as session:
        rows = (await session.execute(select(
            JourneyEnrollment.id, JourneyEnrollment.enrolled_at, JourneyEnrollment.organization_id,
            JourneyEnrollment.next_run_at).where(
            JourneyEnrollment.status.in_(engine.ACTIVE), JourneyEnrollment.next_run_at <= now)
            .order_by(JourneyEnrollment.next_run_at).limit(BATCH))).all()
    if not rows:
        return 0
    if jobs.enabled():
        return await jobs.schedule("journeys.run", [
            ({"enrollment_id": r.id, "enrolled_at": r.enrolled_at.isoformat()},
             f"jr:{r.id}:{int(r.next_run_at.timestamp())}", r.organization_id) for r in rows], queue="outbound")
    for r in rows:
        await engine.run_enrollment(r.id, r.enrolled_at, now)
    return len(rows)


# --- 2. Segmentos -------------------------------------------------------------------------------------------
async def _segment_journeys(session: AsyncSession, segment: Segment) -> list[Journey]:
    rows = (await session.scalars(select(Journey).where(
        Journey.organization_id == segment.organization_id, Journey.status == "active"))).all()
    return [j for j in rows if (j.entry or {}).get("type") in ("segment_enter", "segment_member")
            and int((j.entry or {}).get("segment_id") or 0) == segment.id]


async def enroll_many(session: AsyncSession, journey: Journey, contact_ids: list[int], source: str,
                      data: dict | None = None) -> int:
    n = 0
    for cid in contact_ids:
        if await engine.enroll(session, journey, cid, source, data):
            n += 1
    return n


async def refresh_segment(session: AsyncSession, segment: Segment) -> dict:
    """Recalcula un segmento y mete a los que entraron en sus journeys activos."""
    from app import segments

    result = await segments.refresh(session, segment)
    enrolled = 0
    if result["entered"]:
        for j in await _segment_journeys(session, segment):
            enrolled += await enroll_many(session, j, result["entered"], "segment", {"segment_id": segment.id})
    return {**result, "enrolled": enrolled}


async def refresh_due_segments(now: datetime) -> int:
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Segment).where(Segment.kind == "dynamic"))).all()
        due = [s for s in rows if s.last_computed_at is None
               or s.last_computed_at + timedelta(minutes=s.refresh_minutes) <= now]
        n = 0
        for s in due:
            try:
                await refresh_segment(session, s)
                n += 1
            except Exception:  # noqa: BLE001 — una regla rota no detiene a las demás
                log.exception("No se pudo recalcular el segmento %s", s.id)
                await session.rollback()
        return n


async def on_publish(session: AsyncSession, journey: Journey) -> int:
    """Al activar: «miembro del segmento» inscribe a los miembros actuales; «entra al segmento» solo fija la base."""
    from app import segments

    entry = journey.entry or {}
    if entry.get("type") not in ("segment_enter", "segment_member"):
        return 0
    segment = await session.get(Segment, int(entry["segment_id"]))
    if segment is None:
        return 0
    if segment.kind == "dynamic":
        await segments.refresh(session, segment)
    if entry["type"] == "segment_member":
        ids = await segments.segment_contact_ids(session, segment)
        return await enroll_many(session, journey, ids, "segment", {"segment_id": segment.id})
    return 0


# --- 3. Entradas por fecha ------------------------------------------------------------------------------------
async def _state(session: AsyncSession, org: int) -> OrgSetting:
    row = await session.get(OrgSetting, (org, STATE_KEY))
    if row is None:
        row = OrgSetting(organization_id=org, key=STATE_KEY, value={})
        session.add(row)
    return row


async def date_targets(session: AsyncSession, journey: Journey, today: date) -> list[int]:
    """Clientes cuya fecha (campo de la entrada) cae hoy + desplazamiento: offset −30 = 30 días antes."""
    from app import segments

    entry = journey.entry or {}
    field = entry["field"]
    target = today - timedelta(days=int(entry.get("offset_days") or 0))
    dtype = next((f["type"] for f in await segments.field_catalog(session, journey.organization_id)
                  if f["key"] == field), None)
    if dtype not in ("date", "ts"):
        raise segments.SegmentError(f"{field} no es un campo de fecha")
    value = ([target.isoformat(), target.isoformat()] if dtype == "date"
             else [f"{target.isoformat()}T00:00:00+00:00", f"{target.isoformat()}T23:59:59+00:00"])
    rule: dict = {"all": [{"field": field, "op": "between", "value": value}]}
    ids = await segments.matching_ids(session, journey.organization_id, rule)
    if entry.get("segment_id"):
        seg = await session.get(Segment, int(entry["segment_id"]))
        if seg is not None:
            members = set(await segments.segment_contact_ids(session, seg))
            ids = [i for i in ids if i in members]
    return ids


async def run_date_entries(now: datetime) -> int:
    n = 0
    async with SessionLocal() as session:
        journeys = [j for j in (await session.scalars(select(Journey).where(Journey.status == "active"))).all()
                    if (j.entry or {}).get("type") == "date_field"]
        for j in journeys:
            tz = await engine.org_tz(session, j.organization_id, ((j.settings or {}).get("quiet_hours") or {}).get("timezone"))
            local = now.astimezone(tz)
            if local.strftime("%H:%M") < (j.entry.get("at_time") or "10:00"):
                continue
            state = await _state(session, j.organization_id)
            runs = dict((state.value or {}).get("date_runs") or {})
            if runs.get(str(j.id)) == local.date().isoformat():
                continue
            runs[str(j.id)] = local.date().isoformat()
            state.value = {**(state.value or {}), "date_runs": runs}
            await session.commit()  # marca primero: un fallo a mitad no repite el día
            try:
                ids = await date_targets(session, j, local.date())
                n += await enroll_many(session, j, ids, "date", {"date": local.date().isoformat()})
            except Exception:  # noqa: BLE001
                log.exception("Journey %s: falló la entrada por fecha", j.id)
                await session.rollback()
    return n


# --- 4. Eventos --------------------------------------------------------------------------------------------------
EVENT_SQL = {
    "conversation_closed": (
        "select c.contact_id, ce.occurred_at as at, jsonb_build_object('conversation_id', c.id, "
        "'typification_id', c.typification_id) as data from public.conversation_events ce "
        "join public.conversations c on c.id = ce.conversation_id where ce.organization_id = :org "
        "and ce.event_type = 'closed' and c.typification_id is not null and ce.occurred_at > :lo and ce.occurred_at <= :hi"),
    "deal_won": (
        "select d.contact_id, d.closed_at as at, jsonb_build_object('deal_id', d.id, 'pipeline', d.pipeline, "
        "'amount', d.amount) as data from public.deals d where d.organization_id = :org and d.status = 'won' "
        "and d.closed_at > :lo and d.closed_at <= :hi"),
    "deal_lost": (
        "select d.contact_id, d.closed_at as at, jsonb_build_object('deal_id', d.id, 'pipeline', d.pipeline) as data "
        "from public.deals d where d.organization_id = :org and d.status = 'lost' "
        "and d.closed_at > :lo and d.closed_at <= :hi"),
    "stage_changed": (
        "select d.contact_id, e.created_at as at, jsonb_build_object('deal_id', d.id, 'pipeline', d.pipeline, "
        "'stage', e.to_stage, 'from_stage', e.from_stage) as data from public.deal_stage_events e "
        "join public.deals d on d.id = e.deal_id where e.organization_id = :org "
        "and e.created_at > :lo and e.created_at <= :hi"),
    "appointment_booked": (
        "select a.contact_id, a.created_at as at, jsonb_build_object('appointment_id', a.id) as data "
        "from public.appointments a where a.organization_id = :org and a.created_at > :lo and a.created_at <= :hi"),
    "appointment_no_show": (
        "select a.contact_id, a.updated_at as at, jsonb_build_object('appointment_id', a.id) as data "
        "from public.appointments a where a.organization_id = :org and a.status = 'no_show' "
        "and a.updated_at > :lo and a.updated_at <= :hi"),
    "order_paid": (
        "select o.contact_id, o.updated_at as at, jsonb_build_object('order_id', o.id, 'total', o.total) as data "
        "from public.external_orders o where o.organization_id = :org and o.contact_id is not null "
        "and o.status in ('paid', 'fulfilled') and o.updated_at > :lo and o.updated_at <= :hi"),
}


def matches_filter(entry_filter: dict | None, data: dict) -> bool:
    """Filtro de la entrada por evento: cada clave debe coincidir (lista = uno de)."""
    for k, v in (entry_filter or {}).items():
        if v in (None, "", []):
            continue
        got = data.get(k)
        if isinstance(v, list):
            if got not in v and str(got) not in [str(x) for x in v]:
                return False
        elif str(got) != str(v):
            return False
    return True


async def emit(session: AsyncSession, org: int, contact_id: int, event: str, data: dict | None = None) -> int:
    """Un evento de negocio: inscribe en los journeys con esa entrada y marca metas. Devuelve inscripciones."""
    data = data or {}
    n = 0
    journeys = (await session.scalars(select(Journey).where(
        Journey.organization_id == org, Journey.status == "active"))).all()
    for j in journeys:
        entry = j.entry or {}
        if entry.get("type") == "event" and entry.get("event") == event and matches_filter(entry.get("filter"), data):
            if await engine.enroll(session, j, contact_id, f"event:{event}", data):
                n += 1
    goal = GOAL_OF_EVENT.get(event)
    if goal:
        await engine.goal_reached(session, org, contact_id, goal, data)
    return n


async def poll_events(now: datetime) -> int:
    n = 0
    async with SessionLocal() as session:
        orgs = (await session.scalars(select(Journey.organization_id).where(Journey.status == "active")
                                      .distinct())).all()
        has_orders = await session.scalar(text("select to_regclass('public.external_orders') is not null"))
        for org in orgs:
            state = await _state(session, org)
            cursors = dict((state.value or {}).get("cursors") or {})
            for event, sql in EVENT_SQL.items():
                if event == "order_paid" and not has_orders:
                    continue
                lo = cursors.get(event)
                cursors[event] = now.isoformat()
                if lo is None:  # primera vez: solo eventos desde ahora
                    continue
                try:
                    rows = (await session.execute(text(sql), {"org": org, "lo": datetime.fromisoformat(lo),
                                                              "hi": now})).all()
                except Exception:  # noqa: BLE001
                    log.exception("Journeys: no se pudo leer el evento %s", event)
                    await session.rollback()
                    state = await _state(session, org)
                    continue
                for r in rows:
                    if r.contact_id:
                        n += await emit(session, org, r.contact_id, event, dict(r.data or {}))
            state = await _state(session, org)
            state.value = {**(state.value or {}), "cursors": cursors}
            await session.commit()
    return n
