"""Graph API falsa para pruebas de carga: el backend envía a esta URL en vez de a Meta.

Arranque:
    cd backend && .venv/bin/uvicorn --app-dir ../loadtest fake_meta:app --port 9900 --workers 2
y en el backend de staging:  WA_GRAPH_BASE=http://<host>:9900

Responde como la Cloud API a lo que el backend usa en el camino caliente (enviar mensajes, marcar como leído,
medios, plantillas, perfil) con una latencia configurable (FAKE_META_LATENCY_MS, por defecto 120 ms ± 40 %) y
una tasa de error opcional (FAKE_META_ERROR_RATE, ej. 0.01 → 1 % de 500) para ver reintentos y alertas.
GET /stats devuelve los contadores por ruta.
"""

import asyncio
import itertools
import os
import random
from collections import Counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

LATENCY_MS = float(os.environ.get("FAKE_META_LATENCY_MS", "120"))
ERROR_RATE = float(os.environ.get("FAKE_META_ERROR_RATE", "0"))
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000") + b"\x00" * 20

app = FastAPI(title="Fake Meta Graph API")
ids = itertools.count(1)
hits: Counter = Counter()


async def _latency() -> None:
    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000 * random.uniform(0.6, 1.4))


def _maybe_error() -> JSONResponse | None:
    if ERROR_RATE and random.random() < ERROR_RATE:
        return JSONResponse({"error": {"message": "(fake) Service temporarily unavailable", "code": 2}}, status_code=500)
    return None


@app.get("/stats")
async def stats():
    return dict(hits)


@app.post("/{version}/{node}/messages")
async def send_message(version: str, node: str, request: Request):
    hits["messages"] += 1
    await _latency()
    if err := _maybe_error():
        return err
    body = await request.json()
    if body.get("status") == "read":  # marcar como leído
        return {"success": True}
    recipient = body.get("to") or body.get("recipient") or "unknown"
    contact = {"input": recipient, "wa_id": recipient} if body.get("to") else {"input": recipient, "user_id": recipient}
    return {"messaging_product": "whatsapp", "contacts": [contact],
            "messages": [{"id": f"wamid.fake.{next(ids)}"}]}


@app.get("/media/{media_id}")
async def media_bytes(media_id: str):
    hits["media"] += 1
    return Response(PNG, media_type="image/png")


@app.get("/{version}/{media_id}")
async def media_or_node(version: str, media_id: str, request: Request):
    hits["get_node"] += 1
    await _latency()
    # URL de un medio → el backend la descarga de /media/{id}
    return {"id": media_id, "url": f"{str(request.base_url).rstrip('/')}/media/{media_id}",
            "mime_type": "image/png", "display_phone_number": "+57 300 000 0000", "verified_name": "Load Test",
            "quality_rating": "GREEN", "code_verification_status": "VERIFIED"}


@app.get("/{version}/{waba}/message_templates")
async def templates(version: str, waba: str):
    hits["templates"] += 1
    return {"data": [{"name": "promo", "language": "es", "status": "APPROVED", "category": "MARKETING",
                      "components": [{"type": "BODY", "text": "Hola {{1}}, tenemos {{2}} para ti"}]}]}


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def anything(path: str):
    hits["other"] += 1
    await _latency()
    return {"success": True, "id": f"fake.{next(ids)}"}
