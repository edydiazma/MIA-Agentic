"""Alias /webhooks/meta del webhook de Meta (WhatsApp, Messenger e Instagram comparten el manejador)."""

from app.routers.webhook import meta_router as router

__all__ = ["router"]
