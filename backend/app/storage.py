"""Almacenamiento de archivos: Supabase Storage (buckets privados) o disco local en desarrollo/pruebas.

Las rutas guardadas en la base tienen la forma "<bucket>/<organización>/<...>".
"""

import mimetypes
import uuid
from pathlib import Path

import httpx

from app.config import get_settings

settings = get_settings()
MEDIA_BUCKET = "conversation-media"
RESOURCES_BUCKET = "resources"


def _use_supabase() -> bool:
    return bool(settings.supabase_url and settings.supabase_secret_key)


def _headers(mime: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {settings.supabase_secret_key}", "apikey": settings.supabase_secret_key}
    if mime:
        h["Content-Type"] = mime
    return h


def new_path(bucket: str, org_id: int, folder: str, mime: str) -> str:
    ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ".bin"
    return f"{bucket}/{org_id}/{folder}/{uuid.uuid4().hex}{ext}"


def _local(path: str) -> Path:
    root = Path(settings.media_dir).resolve()
    p = (root / path).resolve()
    if root not in p.parents:
        raise ValueError("ruta fuera del almacenamiento")
    return p


async def upload(path: str, data: bytes, mime: str) -> str:
    if _use_supabase():
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(f"{settings.supabase_url}/storage/v1/object/{path}", content=data,
                                headers={**_headers(mime), "x-upsert": "true"})
            r.raise_for_status()
    else:
        p = _local(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return path


async def download(path: str) -> bytes:
    if _use_supabase():
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.get(f"{settings.supabase_url}/storage/v1/object/{path}", headers=_headers())
            r.raise_for_status()
            return r.content
    return _local(path).read_bytes()


async def remove(path: str) -> None:
    if _use_supabase():
        bucket, _, key = path.partition("/")
        async with httpx.AsyncClient(timeout=60) as http:
            await http.request("DELETE", f"{settings.supabase_url}/storage/v1/object/{bucket}",
                               json={"prefixes": [key]}, headers=_headers("application/json"))
    else:
        _local(path).unlink(missing_ok=True)


def kind_for_mime(mime: str) -> str:
    """Tipo de mensaje de WhatsApp para enviar un archivo con ese MIME."""
    main = mime.split("/")[0]
    return main if main in ("image", "audio", "video") else "document"
