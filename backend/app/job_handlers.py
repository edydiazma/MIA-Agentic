"""Manejadores de la cola de trabajos (app/jobs.py). Cada uno llama a la misma función que antes corría dentro
de su bucle, así el comportamiento no cambia: los bucles ahora solo programan (encolan) y los workers ejecutan en
paralelo, con reintentos y alertas de la cola.

| tipo                   | cola         | qué hace                                                        |
|------------------------|--------------|-----------------------------------------------------------------|
| golden.extract         | media        | lee un documento/imagen con IA (registro maestro)               |
| crm.sync_connection    | crm          | escanear → enviar outbox → traer cambios de HubSpot/Salesforce |
| conversions.upload     | conversions  | un intento de envío a Google Ads / Meta CAPI (backoff propio)   |
| ads.enrich             | default      | nombres de campaña / anuncio de una atribución                  |
"""

from app.jobs import job


@job("golden.extract", queue="media", max_attempts=3)
async def golden_extract(payload: dict) -> dict:
    from app.golden.extract import process_extraction

    ext = await process_extraction(int(payload["extraction_id"]))
    return {"status": getattr(ext, "status", None)}


@job("crm.sync_connection", queue="crm", max_attempts=3)
async def crm_sync_connection(payload: dict) -> dict:
    from app.crm.sync import sync_connection

    result = await sync_connection(int(payload["connection_id"]), force_pull=bool(payload.get("force_pull")))
    return {k: v for k, v in result.items() if k in ("queued", "push", "pull", "pull_error", "skipped")}


@job("conversions.upload", queue="conversions", max_attempts=2)
async def conversions_upload(payload: dict) -> dict:
    """Un intento: process_upload aplica sus propios reintentos (next_attempt_at) y su alerta final."""
    from app.conversions import process_upload
    from app.db import SessionLocal
    from app.models import ConversionUpload, utcnow

    async with SessionLocal() as session:
        up = await session.get(ConversionUpload, int(payload["upload_id"]))
        if not up or up.status != "pending" or (up.next_attempt_at and up.next_attempt_at > utcnow()):
            return {"skipped": True}
        await process_upload(session, up)
        return {"status": up.status, "attempts": up.attempts}


@job("ads.enrich", queue="default", max_attempts=3)
async def ads_enrich(payload: dict) -> dict:
    from app.ad_enrichment import enrich_id

    return {"status": await enrich_id(int(payload["attribution_id"]))}
