"""Contratos de la API (se mantienen estables aunque el modelo de datos sea normalizado)."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

UTCDateTime = Annotated[datetime, AfterValidator(lambda d: d if d.tzinfo else d.replace(tzinfo=UTC))]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AgentOut(ORM):
    id: int
    email: str
    name: str
    role: str
    availability: str
    is_active: bool
    role_id: int | None = None


class AgentCreate(BaseModel):
    email: str
    name: str
    password: str
    role: str = "agent"
    role_id: int | None = None  # rol propio (roles); si viene, manda sobre `role`
    must_change_password: bool = False  # el panel lo marca por defecto al crear con contraseña temporal
    group_ids: list[int] = []


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    role_id: int | None = None
    is_active: bool | None = None
    password: str | None = None
    availability: str | None = None
    group_ids: list[int] | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    agent: AgentOut


class GroupOut(ORM):
    id: int
    name: str
    description: str | None


class ContactOut(BaseModel):
    id: int
    wa_id: str | None  # null: contacto sin WhatsApp (Instagram, Messenger, chat web)
    avatar_url: str | None = None
    name: str | None
    email: str | None = None
    notes: str | None = None
    tags: list[str] = []
    stage: str = "lead"
    custom_fields: dict = {}
    memory: str | None = None
    memory_updated_at: UTCDateTime | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    blocked_at: UTCDateTime | None = None
    marketing_opt_out: bool = False
    created_at: UTCDateTime | None = None
    # Identidad de WhatsApp sin teléfono y métricas de interacción (docs/data-model.md §14)
    wa_username: str | None = None
    wa_bsuid: str | None = None
    channel_providers: list[str] = []
    updated_at: UTCDateTime | None = None
    first_interaction_at: UTCDateTime | None = None
    first_inbound_at: UTCDateTime | None = None
    last_interaction_at: UTCDateTime | None = None
    last_inbound_at: UTCDateTime | None = None
    last_outbound_at: UTCDateTime | None = None
    messages_in: int = 0
    messages_out: int = 0
    conversations_count: int = 0
    flow_runs_count: int = 0
    last_flow_at: UTCDateTime | None = None
    products_count: int = 0
    last_product_name: str | None = None
    lifetime_days: float | None = None
    days_since_last_interaction: float | None = None


class ContactUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    stage: str | None = None
    memory: str | None = None
    custom_fields: dict | None = None  # parcial: solo las claves enviadas; null/"" borra el valor


class ConversationOut(BaseModel):
    id: int
    status: str
    contact: ContactOut
    channel_id: int
    channel_provider: str = "whatsapp_cloud"
    channel_name: str | None = None
    channel_label: str | None = None
    window_open: bool = True  # se puede escribir libremente (si no: plantilla en WhatsApp)
    ai_agent_id: int | None = None
    assigned_agent: AgentOut | None
    group: GroupOut | None
    handoff_reason: str | None
    handoff_at: UTCDateTime | None
    first_response_at: UTCDateTime | None = None
    typification: str | None
    tags: list[str] = []
    ai_summary: str | None = None
    ai_sentiment: str | None = None
    ai_typification: str | None = None
    ai_suggestions: dict | None = None
    ai_classified_at: UTCDateTime | None = None
    closed_at: UTCDateTime | None
    ad_source_type: str | None
    ad_headline: str | None
    unread_count: int
    message_count: int = 0
    last_message_at: UTCDateTime
    last_inbound_at: UTCDateTime | None
    last_message_preview: str | None
    created_at: UTCDateTime


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    direction: str
    sender_type: str
    sender_agent_id: int | None
    type: str
    text: str | None
    media_mime: str | None
    media_filename: str | None
    transcript: str | None
    template_name: str | None
    has_media: bool
    status: str
    error: str | None
    created_at: UTCDateTime


class SendText(BaseModel):
    text: str


class BotOut(BaseModel):
    """Agente de IA (ruta /api/bots por compatibilidad)."""

    id: int
    name: str
    description: str | None = None
    enabled: bool
    cortex_id: int | None
    system_prompt: str
    handoff_message: str
    use_knowledge: bool
    use_memory: bool
    use_customer_memory: bool
    use_catalog: bool
    use_appointments: bool
    # Configuración avanzada (§17)
    timezone: str | None = None
    max_words: int | None = None
    ad_context_enabled: bool = True
    ad_context_prompt: str | None = None
    source_rules: list[dict] = []
    cost_optimization: bool = True
    security_enabled: bool = True
    security_prompt: str | None = None
    security_action: str = "close"
    recovery_enabled: bool = False
    recovery_attempts: list[dict] = []
    inactivity_end_hours: float | None = None
    inactivity_end_typification_id: int | None = None
    extract_field_ids: list[int] = []
    channel_ids: list[int] = []


class BotUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    cortex_id: int | None = None
    system_prompt: str | None = None
    handoff_message: str | None = None
    use_knowledge: bool | None = None
    use_memory: bool | None = None
    use_customer_memory: bool | None = None
    use_catalog: bool | None = None
    use_appointments: bool | None = None
    timezone: str | None = None
    max_words: int | None = None
    ad_context_enabled: bool | None = None
    ad_context_prompt: str | None = None
    source_rules: list[dict] | None = None
    cost_optimization: bool | None = None
    security_enabled: bool | None = None
    security_prompt: str | None = None
    security_action: str | None = None
    recovery_enabled: bool | None = None
    recovery_attempts: list[dict] | None = None
    inactivity_end_hours: float | None = None
    inactivity_end_typification_id: int | None = None
    extract_field_ids: list[int] | None = None
