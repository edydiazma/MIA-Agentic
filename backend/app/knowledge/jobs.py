"""Trabajos de la base de conocimiento (cola «ai»): sincronizar una fuente e indexar un archivo subido."""

from app.jobs import job


@job("knowledge.sync_source", queue="ai", max_attempts=3)
async def knowledge_sync_source(payload: dict) -> dict:
    from app.knowledge.ingest import sync_source

    return await sync_source(int(payload["source_id"]), force=bool(payload.get("force")))


@job("knowledge.index_document", queue="ai", max_attempts=3)
async def knowledge_index_document(payload: dict) -> dict:
    from app.knowledge.ingest import index_document_job

    return await index_document_job(int(payload["document_id"]), force=bool(payload.get("force")))
