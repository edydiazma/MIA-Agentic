"""Chat web: el "envío" es guardar el mensaje; el widget lo recibe por WebSocket (/w/ws) o consultando
/w/messages?after=. La entrega en vivo usa un aviso local por conversación y, como respaldo entre réplicas,
el WebSocket del visitante consulta la base cada pocos segundos."""

import asyncio
import uuid
from collections import defaultdict

from app.channels import ChannelError
from app.models import Channel, Conversation

_waiters: dict[int, set[asyncio.Event]] = defaultdict(set)


def new_id() -> str:
    return f"wc.{uuid.uuid4().hex}"


def notify(conversation_id: int) -> None:
    """Despierta los WebSockets de visitantes de esta conversación en esta réplica."""
    for ev in list(_waiters.get(conversation_id, ())):
        ev.set()


def subscribe(conversation_id: int) -> asyncio.Event:
    ev = asyncio.Event()
    _waiters[conversation_id].add(ev)
    return ev


def unsubscribe(conversation_id: int, ev: asyncio.Event) -> None:
    waiters = _waiters.get(conversation_id)
    if waiters is not None:
        waiters.discard(ev)
        if not waiters:
            _waiters.pop(conversation_id, None)


class WebchatClient:
    provider = "webchat"

    def __init__(self, channel: Channel, conv: Conversation | None):
        self.channel = channel
        self.conv = conv
        self.human = False

    async def send_text(self, to, body: str) -> list[str]:
        return [new_id()]

    async def send_buttons(self, to, body: str, buttons: list[dict]) -> str:
        return new_id()  # los botones van en metadata del mensaje; el widget los muestra como respuestas rápidas

    async def send_list(self, to, body: str, button: str, sections: list[dict]) -> str:
        return new_id()

    async def send_image_link(self, to, link: str, caption: str | None = None) -> str:
        return new_id()

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        return "stored"  # el archivo ya está en Storage (media_path del mensaje)

    async def send_media(self, to, kind: str, media_id: str, caption: str | None = None,
                         filename: str | None = None) -> str:
        return new_id()

    async def send_product(self, to, catalog_id: str, retailer_id: str, body: str) -> str:
        return new_id()

    async def send_template(self, to, name: str, language: str, components: list[dict]) -> str:
        raise ChannelError("Las plantillas son solo de WhatsApp")

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        raise ChannelError("El chat web sube los archivos directamente")

    async def mark_read(self, _message_id: str | None = None) -> None:
        return None
