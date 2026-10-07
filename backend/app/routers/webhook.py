import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from sqlalchemy import update

from app.config import get_settings
from app.db import SessionLocal
from app.ingest import process_webhook
from app.models import InboundEvent, utcnow
from app.channels.meta import org_for_meta_payload
from app.tenancy import org_for_webhook
from app.whatsapp import verify_signature

# WhatsApp, Messenger (object "page") e Instagram (object "instagram") llegan a la misma URL de callback de la app
# de Meta; /webhooks/meta es un alias para quien prefiera registrar Messenger/Instagram aparte.
router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhook"])
meta_router = APIRouter(prefix="/webhooks/meta", tags=["webhook"])
SOURCES = {"page": "messenger", "instagram": "instagram"}
settings = get_settings()
log = logging.getLogger(__name__)


@router.get("")
@meta_router.get("")
async def verify(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
):
    """Verificación inicial que hace Meta al registrar el webhook."""
    if mode == "subscribe" and token == settings.wa_verify_token:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(403, "Token de verificación inválido")


async def _process(event_id: int, received_at, payload: dict) -> None:
    """Procesa y marca el evento crudo (processed_at o error) para poder reprocesarlo."""
    error = None
    try:
        await process_webhook(payload)
    except Exception as e:  # process_webhook ya registra errores por cambio; esto es la red de seguridad
        log.exception("Falló el procesamiento del webhook %s", event_id)
        error = f"{type(e).__name__}: {e}"[:2000]
    async with SessionLocal() as s:
        await s.execute(update(InboundEvent).where(InboundEvent.id == event_id, InboundEvent.received_at == received_at)
                        .values(processed_at=utcnow(), error=error))
        await s.commit()


@router.post("")
@meta_router.post("")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Firma inválida")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "JSON inválido") from None
    # Se guarda el payload crudo (retención corta) y se responde 200 de inmediato; Meta reintenta si tardamos.
    async with SessionLocal() as s:
        # La empresa sale del número (phone_number_id / waba_id) del payload; NULL si no se reconoce
        source = SOURCES.get(payload.get("object"), "whatsapp")
        org = await (org_for_meta_payload(s, payload) if source != "whatsapp" else org_for_webhook(s, payload))
        ev = InboundEvent(organization_id=org, source=source, payload=payload)
        s.add(ev)
        await s.commit()
        event_id, received_at = ev.id, ev.received_at
    background.add_task(_process, event_id, received_at, payload)
    return {"ok": True}
