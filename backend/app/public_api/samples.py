"""Ejemplos de los eventos de webhooks (Zapier los usa para mapear campos antes de la primera entrega real).

La forma es la misma que entrega app.webhooks_out: {"event", "data", "sent_at"}; aquí va solo `data`.
"""

_CONTACT = {"id": 101, "wa_id": "573001234567", "name": "Ana Pérez", "email": "ana@example.com", "notes": None,
            "tags": ["vip"], "stage": "prospect", "custom_fields": {"ciudad": "Bogotá"}, "memory": None,
            "blocked": False, "marketing_opt_out": False, "created_at": "2026-10-11T14:00:00Z"}
_CONVERSATION = {"id": 5001, "status": "human", "contact": _CONTACT, "channel_id": 1, "ai_agent_id": 1,
                 "assigned_agent": {"id": 7, "name": "Luis Gómez", "email": "luis@example.com", "role": "agent"},
                 "group": {"id": 2, "name": "Ventas"}, "handoff_reason": "Quiere cotizar", "typification": None,
                 "tags": ["promo cx5"], "ai_summary": None, "ai_sentiment": "positive", "unread_count": 1,
                 "message_count": 6, "last_message_at": "2026-10-11T14:05:00Z", "created_at": "2026-10-11T14:00:00Z"}
_MESSAGE = {"id": 90001, "conversation_id": 5001, "direction": "in", "sender_type": "contact", "sender_agent_id": None,
            "type": "text", "text": "Hola, quiero la promo de la CX-5", "media_mime": None, "media_filename": None,
            "transcript": None, "template_name": None, "has_media": False, "status": "received", "error": None,
            "created_at": "2026-10-11T14:05:00Z"}

SAMPLES: dict[str, dict] = {
    "message.new": _MESSAGE,
    "message.status": {"id": 90002, "conversation_id": 5001, "status": "read", "error": None},
    "conversation.updated": _CONVERSATION,
    "conversation.handoff": {**_CONVERSATION, "status": "human", "assigned_agent": None},
    "conversation.closed": {**_CONVERSATION, "status": "closed", "typification": "Venta",
                            "closed_at": "2026-10-11T15:00:00Z"},
    "appointment.created": {"id": 301, "contact_id": 101, "conversation_id": 5001, "agent_id": 7,
                            "starts_at": "2026-10-14T15:00:00Z", "ends_at": "2026-10-14T15:30:00Z",
                            "title": "Prueba de manejo", "status": "scheduled"},
    "contact.updated": _CONTACT,
}

EVENT_DESCRIPTIONS = {
    "message.new": "Mensaje nuevo (del cliente, del bot, de un asesor o de una campaña)",
    "message.status": "Cambio de estado de un mensaje enviado (enviado, entregado, leído, fallido)",
    "conversation.updated": "Cambio en una conversación (asignación, grupo, etiquetas, estado)",
    "conversation.handoff": "La conversación pasó del bot a un asesor",
    "conversation.closed": "Conversación cerrada (con su tipificación)",
    "appointment.created": "Cita agendada",
    "contact.updated": "Ficha del contacto editada",
}
