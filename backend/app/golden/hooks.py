"""Disparadores del registro maestro en segundo plano. Nunca lanzan hacia el llamador.

- on_inbound_media(conv, msg): imagen/PDF del cliente → extracción pendiente y lectura inmediata.
- on_close(conversation_id): extracción de la conversación al cerrar (si el clasificador no la hizo).
- golden_loop(): extracciones pendientes cada minuto y oportunidades por vehículo cada 6 horas.
"""

import asyncio
import logging
import time

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
LOOP_EVERY_S = 60
OPPORTUNITIES_EVERY_S = 6 * 3600


def _spawn(coro, name: str) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def on_inbound_media(session, conv, msg) -> None:
    """Llamado desde ingest con cada mensaje del cliente (cualquier canal)."""
    try:
        from app.golden.extract import enqueue_message, is_document_media, process_extraction

        if not is_document_media(getattr(msg, "media_mime", None)):
            return
        ext_id = await enqueue_message(session, conv, msg)
        if ext_id:
            _spawn(process_extraction(ext_id), f"golden-doc-{ext_id}")
    except Exception:  # noqa: BLE001
        log.exception("No se pudo encolar la lectura del documento del mensaje %s", getattr(msg, "id", None))


def on_close(conversation_id: int) -> None:
    async def job():
        try:
            from app.golden.extract import extract_on_close

            await extract_on_close(conversation_id)
        except Exception:  # noqa: BLE001
            log.exception("Extracción del registro maestro al cerrar %s", conversation_id)

    _spawn(job(), f"golden-close-{conversation_id}")


async def golden_loop() -> None:
    from app.golden.extract import process_pending
    from app.golden.opportunities import run_all

    last_opps = 0.0
    while True:
        try:
            await process_pending()
            if time.monotonic() - last_opps >= OPPORTUNITIES_EVERY_S:
                last_opps = time.monotonic()
                created = await run_all()
                if created:
                    log.info("Oportunidades por vencimientos de vehículos: %s", created)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Bucle del registro maestro")
        await asyncio.sleep(LOOP_EVERY_S)
