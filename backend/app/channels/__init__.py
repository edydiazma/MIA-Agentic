"""Canales de mensajería: WhatsApp, Messenger, Instagram y chat web detrás de una sola interfaz.

Todos los clientes exponen los métodos del cliente de WhatsApp (send_text, send_media, upload_media,
download_media, send_buttons, send_list, send_image_link, send_product, send_template, mark_read) con el mismo
primer argumento `to`. Los clientes que no son de WhatsApp se crean ligados a una conversación: si `to` viene
vacío (el contacto no tiene número) usan la identidad del contacto en ese canal (contact_identities).
docs/data-model.md §12.2
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, ContactIdentity, Conversation

WHATSAPP = "whatsapp_cloud"
META_PROVIDERS = ("messenger", "instagram")
PROVIDERS = (WHATSAPP, "messenger", "instagram", "webchat", "email")
LABELS = {WHATSAPP: "WhatsApp", "messenger": "Messenger", "instagram": "Instagram", "webchat": "Chat web",
          "email": "Correo"}

# Ventana para responder libremente después del último mensaje del cliente.
# Messenger e Instagram: 24 h estándar y hasta 7 días con la etiqueta HUMAN_AGENT (solo respuestas de personas).
STANDARD_WINDOW = timedelta(hours=24)
HUMAN_AGENT_WINDOW = timedelta(days=7)


class ChannelError(Exception):
    pass


def window_for(provider: str, human: bool) -> timedelta | None:
    """Ventana de respuesta libre; None = sin límite (chat web y correo)."""
    if provider in ("webchat", "email"):
        return None
    if provider in META_PROVIDERS and human:
        return HUMAN_AGENT_WINDOW
    return STANDARD_WINDOW


async def identity_for(session: AsyncSession, conv: Conversation, channel: Channel) -> ContactIdentity | None:
    q = select(ContactIdentity).where(ContactIdentity.contact_id == conv.contact_id,
                                      ContactIdentity.provider == channel.provider)
    if channel.provider != WHATSAPP:
        q = q.where(ContactIdentity.channel_id == channel.id)
    return (await session.scalars(q.order_by(ContactIdentity.id.desc()).limit(1))).first()


async def channel_client(session: AsyncSession, channel: Channel, conv: Conversation | None = None):
    """Cliente del canal. Para Messenger, Instagram y chat web conviene pasar la conversación."""
    from app.secrets_vault import get_secret

    if channel.provider == WHATSAPP:
        from app.whatsapp import WhatsAppClient

        return WhatsAppClient(channel.phone_number_id, await get_secret(session, channel.access_token_secret_id))
    recipient = None
    last_inbound = None
    if conv is not None:
        ident = await identity_for(session, conv, channel)
        recipient = ident.external_id if ident else None
        last_inbound = conv.last_inbound_at
    if channel.provider in META_PROVIDERS:
        from app.channels.meta import MetaClient

        return MetaClient(channel, await get_secret(session, channel.access_token_secret_id), recipient, last_inbound)
    if channel.provider == "webchat":
        from app.channels.webchat import WebchatClient

        return WebchatClient(channel, conv)
    if channel.provider == "email":
        from app.channels.email import EmailClient

        return EmailClient(session, channel, conv, recipient)
    raise ChannelError(f"Proveedor de canal desconocido: {channel.provider}")
