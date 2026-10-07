"""Ganchos del onboarding en el resto del sistema: respuesta de prueba entrante y validación periódica."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Channel, ChannelHealthCheck, OnboardingRun, WaTemplate, utcnow
from app.onboarding import checks, graph, meta
from app.onboarding.service import advance, deep_merge, step, steps_of

log = logging.getLogger(__name__)
FULL_EVERY = timedelta(hours=6)
TEMPLATES_EVERY_S = 600


async def on_whatsapp_inbound(session: AsyncSession, channel: Channel, wa_id: str) -> None:
    """Llamado por ingest con cada mensaje de WhatsApp: si es la respuesta al mensaje de prueba del asistente,
    marca la prueba de ida y vuelta (los webhooks llegan) y el paso «Prueba»."""
    run = (await session.scalars(select(OnboardingRun).where(
        OnboardingRun.organization_id == channel.organization_id, OnboardingRun.status == "in_progress")
        .limit(1))).first()
    test = ((run.answers if run else {}) or {}).get("test") or {}
    if not run or test.get("replied") or test.get("phone") != wa_id:
        return
    run.answers = deep_merge(run.answers, {"test": {"replied": True, "replied_at": utcnow().isoformat()}})
    row = await session.get(ChannelHealthCheck, (channel.id, "inbound_roundtrip"))
    if row is None:
        row = ChannelHealthCheck(channel_id=channel.id, check_key="inbound_roundtrip",
                                 organization_id=channel.organization_id)
        session.add(row)
    row.status, row.detail, row.fixable, row.checked_at = "pass", "Respuesta recibida: los webhooks llegan", False, utcnow()
    s = await step(session, run, "test")
    s.status, s.finished_at = "done", utcnow()
    s.result = {**(s.result or {}), "replied": True}
    advance(run, await steps_of(session, run))


async def validate_all(full: bool) -> int:
    """full: todos los chequeos de cada número; si no, solo el estado de plantillas pendientes."""
    done = 0
    async with SessionLocal() as session:
        if full:
            channels = (await session.scalars(select(Channel).where(
                Channel.provider == "whatsapp_cloud", Channel.status != "disconnected",
                Channel.waba_id.is_not(None)))).all()
        else:
            ids = (await session.scalars(select(WaTemplate.waba_id).where(
                WaTemplate.source == "onboarding", WaTemplate.status.in_(("PENDING", "IN_APPEAL"))).distinct())).all()
            channels = (await session.scalars(select(Channel).where(
                Channel.provider == "whatsapp_cloud", Channel.waba_id.in_(ids)))).all() if ids else []
        for channel in channels:
            try:
                if full:
                    run = (await session.scalars(select(OnboardingRun).where(
                        OnboardingRun.organization_id == channel.organization_id)
                        .order_by(OnboardingRun.id.desc()).limit(1))).first()
                    await checks.run_checks(session, channel, run)
                else:
                    token = await meta.channel_token(session, channel)
                    if token:
                        await meta.refresh_template_statuses(session, channel, token)
                await session.commit()
                done += 1
            except graph.GraphError as e:
                await session.rollback()
                log.info("Validación del número %s: %s", channel.id, e)
    return done


async def health_checks_loop() -> None:
    """Registrado en main.LOOPS: plantillas pendientes cada 10 min, validación completa cada 6 h."""
    last_full = utcnow() - FULL_EVERY
    while True:
        await asyncio.sleep(TEMPLATES_EVERY_S)
        try:
            full = utcnow() - last_full >= FULL_EVERY
            await validate_all(full)
            if full:
                last_full = utcnow()
        except Exception:
            log.exception("Falló la validación periódica de números")
