"""Trabajos del hub en la cola (app/jobs.py).

| tipo                | cola    | qué hace                                                            |
|---------------------|---------|---------------------------------------------------------------------|
| hub.sync_connection | crm     | tienda (productos/pedidos), calendario (push + pull) o conector propio |
| hub.export          | default | una corrida de exportación de datos                                  |
"""

from app.jobs import job


@job("hub.sync_connection", queue="crm", max_attempts=3)
async def hub_sync_connection(payload: dict) -> dict:
    from app.hub.runner import sync_connection

    result = await sync_connection(int(payload["connection_id"]), full=bool(payload.get("full")))
    return {k: (v if isinstance(v, (int, str, bool, type(None))) else str(v)[:200]) for k, v in result.items()}


@job("hub.export", queue="default", max_attempts=2)
async def hub_export(payload: dict) -> dict:
    from app.hub.runner import run_export

    return await run_export(int(payload["export_id"]), full=bool(payload.get("full")))
