"""Transcripción de notas de voz (Claude no recibe audio, así que se pasa a texto)."""

import logging

import openai

from app.config import get_settings

settings = get_settings()
log = logging.getLogger(__name__)


async def transcribe(data: bytes, mime: str) -> str | None:
    if not settings.transcribe_model or not settings.openai_api_key:
        return None
    ext = "ogg" if "ogg" in mime else mime.split("/")[-1].split(";")[0]
    try:
        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        result = await client.audio.transcriptions.create(
            model=settings.transcribe_model, file=(f"audio.{ext}", data, mime.split(";")[0])
        )
        return result.text.strip() or None
    except Exception:
        log.exception("Falló la transcripción del audio")
        return None
