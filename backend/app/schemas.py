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


class AgentCreate(BaseModel):
    email: str
    name: str
    password: str
    role: str = "agent"
    group_ids: list[int] = []


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
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
    wa_id: str
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
