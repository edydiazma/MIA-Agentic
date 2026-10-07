"""Eventos que llegan de afuera a los journeys (llamados desde app/ingest.py, siempre protegidos con try).

- `on_status`: estado de un mensaje enviado por un journey → evento delivered / read / failed (una vez por mensaje).
- `on_inbound`: el cliente escribe → evento replied en sus inscripciones activas que ya le enviaron algo, meta
  «replied», salida por opt-out / bloqueo, y despierta a las que esperan una condición.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.journeys import engine
from app.models import Conversation, JourneyEnrollment, JourneyEvent, Message, utcnow

STATUS_KIND = {"delivered": "delivered", "read": "read", "failed": "failed"}


async def _wake(session: AsyncSession, e: JourneyEnrollment) -> None:
    """Una inscripción esperando una condición se evalúa ya (no al vencer el plazo)."""
    if e.status == "waiting":
        e.next_run_at = utcnow()


async def on_status(session: AsyncSession, msg: Message, status: str) -> None:
    kind = STATUS_KIND.get(status)
    if kind is None:
        return
    sent = (await session.scalars(select(JourneyEvent).where(
        JourneyEvent.message_id == msg.id, JourneyEvent.kind == "sent").limit(1))).first()
    if sent is None:
        return
    kinds = ["delivered", "read"] if kind == "read" else [kind]  # leído implica entregado
    existing = set((await session.scalars(select(JourneyEvent.kind).where(
        JourneyEvent.message_id == msg.id, JourneyEvent.kind.in_(kinds)))).all())
    e = await engine.get_enrollment(session, sent.enrollment_id)
    if e is None:
        return
    for k in kinds:
        if k not in existing:
            await engine.add_event(session, e, sent.step_id, k, message_id=msg.id)
    if kind == "read":
        await _wake(session, e)
    await session.commit()
    if kind == "read" and e.status == "waiting":
        await engine.schedule_run(session, e)


async def on_inbound(session: AsyncSession, conv: Conversation, msg: Message) -> None:
    if msg.direction != "in":
        return
    rows = (await session.scalars(select(JourneyEnrollment).where(
        JourneyEnrollment.organization_id == conv.organization_id, JourneyEnrollment.contact_id == conv.contact_id,
        JourneyEnrollment.status.in_(engine.ACTIVE)))).all()
    if not rows:
        return
    contact = conv.contact
    woke: list[JourneyEnrollment] = []
    for e in rows:
        if contact is not None and (contact.blocked or contact.marketing_opt_out):
            await engine.finish(session, e, "exited", "blocked" if contact.blocked else "opt_out")
            continue
        last_sent = (await session.scalars(select(JourneyEvent).where(
            JourneyEvent.enrollment_id == e.id, JourneyEvent.kind == "sent")
            .order_by(JourneyEvent.created_at.desc()).limit(1))).first()
        if last_sent is None:  # todavía no le enviamos nada: no es una respuesta al journey
            continue
        already = await session.scalar(select(JourneyEvent.id).where(
            JourneyEvent.enrollment_id == e.id, JourneyEvent.kind == "replied",
            JourneyEvent.created_at >= last_sent.created_at).limit(1))
        if not already:
            await engine.add_event(session, e, last_sent.step_id, "replied", message_id=msg.id)
        await _wake(session, e)
        woke.append(e)
    await session.commit()
    await engine.goal_reached(session, conv.organization_id, conv.contact_id, "replied")
    for e in woke:
        if e.status == "waiting":
            await engine.schedule_run(session, e)
