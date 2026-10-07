"""Ejecución del hub: sincronizar una conexión (tienda, calendario, conector propio), correr una exportación y el
bucle programador (encola en la cola de trabajos si está activa; si no, ejecuta en línea)."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.hub import builder, calendars, commerce, common, exports
from app.hub.http import HubError
from app.models import DataExport, IntegrationConnection, utcnow

log = logging.getLogger(__name__)
HUB_PROVIDERS = (*common.COMMERCE, *common.CALENDARS, "custom")
DEFAULT_MINUTES = {"shopify": 15, "woocommerce": 15, "vtex": 15, "google_calendar": 2, "microsoft_calendar": 2,
                   "custom": 30}


async def sync_connection(conn_id: int, full: bool = False) -> dict:
    async with SessionLocal() as session:
        conn = await session.get(IntegrationConnection, conn_id)
        if not conn or conn.status not in ("connected", "error") or conn.provider not in HUB_PROVIDERS:
            return {"skipped": True}
        try:
            if conn.provider in common.COMMERCE:
                return await commerce.sync_store(session, conn, full=full)
            if conn.provider in common.CALENDARS:
                pushed = await calendars.push_pending(session, conn.organization_id)
                pulled = await calendars.pull_changes(session, conn)
                return {"push": pushed, "pull": pulled}
            defn = (await builder.load(session, conn))[0]
            return {e: await builder.sync_entity(session, conn, e)
                    for e in builder.ENTITIES if ((defn.mappings or {}).get(e) or {}).get("pull")}
        except HubError as e:
            await session.rollback()
            conn = await session.get(IntegrationConnection, conn_id)
            if conn and not e.retryable:
                conn.status, conn.last_error = "error", str(e)[:500]
                await session.commit()
            if e.retryable:
                raise
            return {"error": str(e)}


async def run_export(export_id: int, full: bool = False) -> dict:
    async with SessionLocal() as session:
        export = await session.get(DataExport, export_id)
        if not export:
            return {"skipped": True}
        run = await exports.run_export(session, export, full=full)
        return {"status": run.status, "rows": run.rows_exported, "error": run.error}


def _due(conn: IntegrationConnection, now) -> bool:
    minutes = int((conn.settings or {}).get("sync_minutes") or DEFAULT_MINUTES.get(conn.provider, 30))
    return conn.last_sync_at is None or now - conn.last_sync_at >= timedelta(minutes=minutes) - timedelta(seconds=20)


async def tick() -> dict:
    """Un ciclo del programador: conexiones del hub vencidas y exportaciones vencidas."""
    from app import jobs

    now = utcnow()
    async with SessionLocal() as session:
        conns = [c for c in (await session.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider.in_(HUB_PROVIDERS), IntegrationConnection.status == "connected",
            IntegrationConnection.sync_enabled))).all() if _due(c, now)]
        conn_rows = [(c.id, c.organization_id) for c in conns
                     if c.provider != "custom" or c.connector_id is not None]
        due_exports = [(e.id, e.organization_id) for e in (await session.scalars(select(DataExport).where(
            DataExport.is_active))).all() if exports.due(e, now)]
    if jobs.enabled():
        await jobs.schedule("hub.sync_connection", [({"connection_id": i}, f"hub:{i}", o) for i, o in conn_rows],
                            queue="crm")
        await jobs.schedule("hub.export", [({"export_id": i}, f"export:{i}", o) for i, o in due_exports])
    else:
        for i, _ in conn_rows:
            try:
                await sync_connection(i)
            except Exception:
                log.exception("Hub: falló la sincronización de la conexión %s", i)
        for i, _ in due_exports:
            try:
                await run_export(i)
            except Exception:
                log.exception("Hub: falló la exportación %s", i)
    return {"connections": len(conn_rows), "exports": len(due_exports)}


async def hub_loop() -> None:
    """Worker: cada 60 s programa sincronizaciones (tiendas, calendarios, conectores propios) y exportaciones."""
    while True:
        await asyncio.sleep(60)
        try:
            await tick()
        except Exception:
            log.exception("Falló el ciclo del hub de integraciones")
