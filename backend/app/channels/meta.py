"""Messenger e Instagram (Messenger Platform de Meta).

Envío: POST https://graph.facebook.com/{versión}/{page_id}/messages con el token de la página. Instagram usa la
misma Send API a través de la página de Facebook vinculada a la cuenta profesional (recipient = IGSID).
Ventana: RESPONSE dentro de 24 h del último mensaje del cliente; después, hasta 7 días, MESSAGE_TAG HUMAN_AGENT
(solo respuestas de personas). Webhooks: object "page" (Messenger) u "instagram", entry[].messaging[].
"""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import HUMAN_AGENT_WINDOW, STANDARD_WINDOW, ChannelError
from app.config import get_settings
from app.models import Channel

log = logging.getLogger(__name__)
settings = get_settings()

MAX_TEXT = {"messenger": 2000, "instagram": 1000}
MAX_QUICK_REPLIES = 13
ATTACHMENT_TYPES = {"image": "image", "video": "video", "audio": "audio", "document": "file"}


def graph_base() -> str:
    return f"https://graph.facebook.com/{settings.wa_api_version}"


def http_client(timeout: float = 30) -> httpx.AsyncClient:
    """Punto único para simular la Graph API en pruebas."""
    return httpx.AsyncClient(timeout=timeout)


class MetaClient:
    def __init__(self, channel: Channel, token: str | None, recipient: str | None = None,
                 last_inbound_at: datetime | None = None):
        self.channel = channel
        self.provider = channel.provider
        self.page_id = channel.page_id or channel.external_id
        self.token = token
        self.recipient = recipient
        self.last_inbound_at = last_inbound_at
        self.human = False  # True cuando responde un asesor (habilita HUMAN_AGENT)

    # -- utilidades
    def _to(self, to: str | None) -> str:
        rid = self.recipient or to
        if not rid:
            raise ChannelError(f"El contacto no tiene identidad en {self.provider}")
        return rid

    def _messaging_type(self) -> dict:
        last = self.last_inbound_at
        if last is None:
            return {"messaging_type": "RESPONSE"}
        age = datetime.now(UTC) - (last if last.tzinfo else last.replace(tzinfo=UTC))
        if age < STANDARD_WINDOW:
            return {"messaging_type": "RESPONSE"}
        if self.human and age < HUMAN_AGENT_WINDOW:
            return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
        raise ChannelError(f"Pasó la ventana de respuesta de {self.provider} (24 h; 7 días para asesores)")

    async def _send(self, to: str | None, message: dict) -> str:
        if not self.token:
            raise ChannelError("Falta el token de la página: conéctala en Configuraciones → Plataforma")
        body = {"recipient": {"id": self._to(to)}, "message": message, **self._messaging_type()}
        async with http_client() as http:
            r = await http.post(f"{graph_base()}/{self.page_id}/messages", json=body,
                                params={"access_token": self.token})
        if r.status_code >= 400:
            raise ChannelError(r.text[:1000])
        return r.json().get("message_id") or ""

    # -- interfaz común
    async def send_text(self, to: str | None, body: str) -> list[str]:
        from app.whatsapp import split_text

        return [await self._send(to, {"text": chunk}) for chunk in split_text(body, MAX_TEXT[self.provider])]

    async def send_buttons(self, to: str | None, body: str, buttons: list[dict]) -> str:
        return await self._quick_replies(to, body, [b["title"] for b in buttons])

    async def send_list(self, to: str | None, body: str, button: str, sections: list[dict]) -> str:
        titles = [r["title"] for s in sections for r in s.get("rows", [])]
        return await self._quick_replies(to, body, titles)

    async def _quick_replies(self, to: str | None, body: str, titles: list[str]) -> str:
        qr = [{"content_type": "text", "title": t[:20], "payload": t[:1000]} for t in titles[:MAX_QUICK_REPLIES] if t]
        return await self._send(to, {"text": body[:MAX_TEXT[self.provider]] or "…", "quick_replies": qr})

    async def send_image_link(self, to: str | None, link: str, caption: str | None = None) -> str:
        mid = await self._send(to, {"attachment": {"type": "image", "payload": {"url": link, "is_reusable": True}}})
        if caption:
            await self.send_text(to, caption)
        return mid

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        """Sube el archivo como adjunto reutilizable; devuelve "tipo:attachment_id"."""
        from app.storage import kind_for_mime

        kind = ATTACHMENT_TYPES[kind_for_mime(mime)]
        if not self.token:
            raise ChannelError("Falta el token de la página")
        async with http_client(60) as http:
            r = await http.post(f"{graph_base()}/{self.page_id}/message_attachments",
                                params={"access_token": self.token},
                                data={"message": '{"attachment": {"type": "%s", "payload": {"is_reusable": true}}}' % kind},
                                files={"filedata": (filename, data, mime)})
        if r.status_code >= 400:
            raise ChannelError(r.text[:1000])
        return f"{kind}:{r.json()['attachment_id']}"

    async def send_media(self, to: str | None, kind: str, media_id: str, caption: str | None = None,
                         filename: str | None = None) -> str:
        att_type, _, att_id = media_id.partition(":")
        mid = await self._send(to, {"attachment": {"type": att_type or ATTACHMENT_TYPES.get(kind, "file"),
                                                   "payload": {"attachment_id": att_id or media_id}}})
        if caption:
            await self.send_text(to, caption)
        return mid

    async def send_product(self, to: str | None, catalog_id: str, retailer_id: str, body: str) -> str:
        raise ChannelError("Los mensajes de catálogo solo existen en WhatsApp")

    async def send_template(self, to: str | None, name: str, language: str, components: list[dict]) -> str:
        raise ChannelError("Las plantillas son solo de WhatsApp")

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Los adjuntos entrantes traen una URL firmada del CDN de Meta."""
        async with http_client(60) as http:
            r = await http.get(url)
        if r.status_code >= 400:
            raise ChannelError(f"No se pudo descargar el adjunto ({r.status_code})")
        return r.content, r.headers.get("content-type", "application/octet-stream")

    async def mark_read(self, _message_id: str | None = None) -> None:
        if not (self.token and self.recipient):
            return
        try:
            async with http_client(15) as http:
                await http.post(f"{graph_base()}/{self.page_id}/messages", params={"access_token": self.token},
                                json={"recipient": {"id": self.recipient}, "sender_action": "mark_seen"})
        except httpx.HTTPError:
            log.debug("mark_seen falló", exc_info=True)

    async def user_profile(self, user_id: str) -> dict:
        """Nombre y foto del usuario (best effort; requiere permisos de perfil)."""
        fields = "name,profile_pic" if self.provider == "messenger" else "name,username,profile_pic"
        try:
            async with http_client(15) as http:
                r = await http.get(f"{graph_base()}/{user_id}", params={"fields": fields, "access_token": self.token})
            return r.json() if r.status_code < 400 else {}
        except (httpx.HTTPError, ValueError):
            return {}


async def subscribe_page(page_id: str, token: str) -> None:
    """Suscribe la app a los eventos de la página (Messenger e Instagram usan la misma suscripción)."""
    fields = "messages,messaging_postbacks,messaging_referrals,message_reads,message_deliveries,message_echoes"
    async with http_client() as http:
        r = await http.post(f"{graph_base()}/{page_id}/subscribed_apps",
                            params={"subscribed_fields": fields, "access_token": token})
    if r.status_code >= 400:
        raise ChannelError(f"Meta rechazó la suscripción de la página: {r.text[:500]}")


def entry_ids(payload: dict) -> list[str]:
    return [str(e.get("id")) for e in payload.get("entry", []) if e.get("id")]


async def channel_for_entry(session: AsyncSession, provider: str, entry_id: str) -> Channel | None:
    return (await session.scalars(select(Channel).where(
        Channel.provider == provider, or_(Channel.external_id == entry_id, Channel.page_id == entry_id))
        .limit(1))).first()


async def org_for_meta_payload(session: AsyncSession, payload: dict) -> int | None:
    provider = {"page": "messenger", "instagram": "instagram"}.get(payload.get("object"))
    if not provider:
        return None
    orgs = set()
    for eid in entry_ids(payload):
        ch = await channel_for_entry(session, provider, eid)
        if ch:
            orgs.add(ch.organization_id)
    return orgs.pop() if len(orgs) == 1 else None
