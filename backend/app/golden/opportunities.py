"""Oportunidades desde el registro maestro: vencimientos del vehículo del cliente (docs/data-model.md §15).

SOAT → línea "seguros" (origin soat_due); revisión técnico-mecánica → "taller" (inspection_due); próximo servicio →
"taller" (service_due). Una oportunidad por vehículo + origen + fecha de vencimiento (idempotente); dueño: el último
asesor del cliente, con un seguimiento opcional.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ContactVehicle, Deal, FollowUp, Organization
from app.settings_store import get_setting

RULES = (  # (columna de vencimiento, origin, pipeline, etiqueta)
    ("insurance_due", "soat_due", "seguros", "Renovación SOAT"),
    ("inspection_due", "inspection_due", "taller", "Revisión técnico-mecánica"),
    ("next_service_at", "service_due", "taller", "Mantenimiento"),
)


async def vehicle_opportunities(session: AsyncSession, org: int, today: date | None = None) -> int:
    settings = await get_setting(session, "golden", org)
    if not settings["auto_opportunities"]:
        return 0
    today = today or datetime.now(UTC).date()
    horizon = today + timedelta(days=int(settings["opportunity_days"]))
    created = 0
    for column, origin, pipeline, label in RULES:
        col = getattr(ContactVehicle, column)
        vehicles = (await session.scalars(select(ContactVehicle).where(
            ContactVehicle.organization_id == org, ContactVehicle.status == "active", col.is_not(None),
            col >= today, col <= horizon))).all()
        for v in vehicles:
            due = getattr(v, column)
            existing = (await session.scalars(select(Deal.id).where(
                Deal.vehicle_id == v.id, Deal.origin == origin,
                or_(Deal.attributes["due_date"].astext == due.isoformat(), Deal.status == "open")))).first()
            if existing:
                continue
            contact = await session.get(Contact, v.contact_id)
            ident = v.plate or v.vin
            vehicle_name = " ".join(x for x in (v.make, v.model, str(v.year) if v.year else None) if x)
            deal = Deal(organization_id=org, contact_id=v.contact_id, owner_agent_id=contact.last_agent_id,
                        name=f"{label} {ident} (vence {due.isoformat()})", pipeline=pipeline, stage="new",
                        status="open", source="ai", vehicle_id=v.id, origin=origin, expected_close=due,
                        attributes={"due_date": due.isoformat(), "plate": v.plate, "vin": v.vin,
                                    "vehicle": vehicle_name or None})
            session.add(deal)
            await session.flush()
            created += 1
            if settings["followup_on_opportunity"] and contact.last_agent_id:
                session.add(FollowUp(organization_id=org, contact_id=v.contact_id, agent_id=contact.last_agent_id,
                                     due_at=datetime.combine(max(today, due - timedelta(days=7)), time(9), UTC),
                                     note=f"{label}: {ident} vence el {due.isoformat()}"))
            try:
                from app.crm.sync import enqueue_deal

                await enqueue_deal(session, deal.id)
            except (ImportError, AttributeError):
                pass
    await session.commit()
    return created


async def run_all() -> int:
    from app.db import SessionLocal

    total = 0
    async with SessionLocal() as session:
        orgs = (await session.scalars(select(Organization.id).where(
            Organization.status.in_(("trial", "active"))))).all()
    for org in orgs:
        async with SessionLocal() as session:
            total += await vehicle_opportunities(session, org)
    return total
