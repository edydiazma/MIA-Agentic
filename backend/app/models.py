"""Modelos ORM: espejo exacto de supabase/migrations (fuente de verdad del esquema).

Reglas:
- El esquema lo crean SOLO las migraciones SQL; aquí no se usa create_all.
- Contadores, eventos de conversación, cierre/reapertura y primera respuesta los mantiene la base
  (triggers). El backend no debe duplicar esa lógica.
- Tablas particionadas (messages, conversation_events, ai_calls, flow_runs, ...) tienen PK compuesta (id, fecha).
"""

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def ts(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


def pk():
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def org_fk():
    return mapped_column(BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"))


def fk(target: str, **kw):
    return mapped_column(BigInteger, ForeignKey(target, **kw), nullable=True)


# =============================================================================
# 01 · Núcleo
# =============================================================================
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="America/Bogota")
    plan: Mapped[str] = mapped_column(Text, default="internal")
    # SaaS (migración 14)
    status: Mapped[str] = mapped_column(Text, default="active")  # trial | active | past_due | suspended | cancelled
    plan_id: Mapped[int | None] = fk("plans.id")
    trial_ends_at: Mapped[datetime | None] = ts(nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    billing_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    onboarding_completed_at: Mapped[datetime | None] = ts(nullable=True)  # null = mostrar el asistente
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Agent(Base):
    """Usuario humano (asesor, supervisor o administrador)."""

    __tablename__ = "agents"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    auth_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, default="agent")  # admin | supervisor | agent
    availability: Mapped[str] = mapped_column(Text, default="available")  # available | away | busy
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # también criterio de enrutamiento IA
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class AgentGroup(Base):
    __tablename__ = "agent_groups"

    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)


class OrgSetting(Base):
    __tablename__ = "org_settings"

    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_by: Mapped[int | None] = fk("agents.id")
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ConfigRevision(Base):
    """Historial JSON de entidades editables por personas o por IA."""

    __tablename__ = "config_revisions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(Text)  # human | ai | import | system
    ai_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# 02 · Canales, contactos, campos, etiquetas
# =============================================================================
class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, default="whatsapp_cloud")  # whatsapp_cloud|messenger|instagram|webchat
    phone_number_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # solo WhatsApp
    waba_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    default_ai_agent_id: Mapped[int | None] = fk("ai_agents.id")
    # Llamadas de WhatsApp (migración 13)
    calling_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    calling_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    voice_agent_id: Mapped[int | None] = fk("voice_agents.id")
    # Omnicanal (migración 18)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # page id / IG account id / widget key
    page_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="active")  # active | disconnected | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Estado de Meta del número (migración 21)
    verified_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_verification_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    messaging_limit_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    throughput_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_coexistence: Mapped[bool] = mapped_column(Boolean, default=False)
    pin_set_at: Mapped[datetime | None] = ts(nullable=True)
    registered_at: Mapped[datetime | None] = ts(nullable=True)
    webhook_subscribed_at: Mapped[datetime | None] = ts(nullable=True)
    last_inbound_at: Mapped[datetime | None] = ts(nullable=True)
    business_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    meta_synced_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    ai_agent: Mapped["AIAgent | None"] = relationship(lazy="joined", foreign_keys=[default_ai_agent_id])


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    wa_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # null: contacto sin WhatsApp (IG, Messenger, web)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Identidad de WhatsApp sin teléfono (migración 22): BSUID por portafolio y @usuario
    wa_bsuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_parent_bsuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Métricas de interacción (las mantienen triggers; solo lectura para la app). Las FK existen en la base;
    # en el ORM no se declaran para no volver ambiguos los joins contacts↔conversations/agents/channels.
    first_interaction_at: Mapped[datetime | None] = ts(nullable=True)
    first_inbound_at: Mapped[datetime | None] = ts(nullable=True)
    last_interaction_at: Mapped[datetime | None] = ts(nullable=True)
    last_inbound_at: Mapped[datetime | None] = ts(nullable=True)
    last_outbound_at: Mapped[datetime | None] = ts(nullable=True)
    messages_in: Mapped[int] = mapped_column(Integer, default=0)
    messages_out: Mapped[int] = mapped_column(Integer, default=0)
    conversations_count: Mapped[int] = mapped_column(Integer, default=0)
    first_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_typification_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_typified_at: Mapped[datetime | None] = ts(nullable=True)
    flow_runs_count: Mapped[int] = mapped_column(Integer, default=0)
    last_flow_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    last_flow_at: Mapped[datetime | None] = ts(nullable=True)
    channel_providers: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    channel_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)  # números de la empresa usados
    products_count: Mapped[int] = mapped_column(Integer, default=0)
    last_product_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fuente del cliente: primer y último toque (migración 24; las mantiene un trigger sobre attributions)
    first_source_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_source_ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_source_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_source_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_source_at: Mapped[datetime | None] = ts(nullable=True)
    first_attribution_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_source_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_source_ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_source_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_source_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_source_at: Mapped[datetime | None] = ts(nullable=True)
    last_attribution_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(Text, default="lead")
    memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_updated_at: Mapped[datetime | None] = ts(nullable=True)
    marketing_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opt_out_at: Mapped[datetime | None] = ts(nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_at: Mapped[datetime | None] = ts(nullable=True)
    last_seen_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    field_values: Mapped[list["ContactFieldValue"]] = relationship(lazy="selectin", viewonly=True)
    tag_links: Mapped[list["ContactTag"]] = relationship(lazy="selectin", viewonly=True)


class ContactField(Base):
    __tablename__ = "contact_fields"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text, default="text")
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_extract: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_editable: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    archived_at: Mapped[datetime | None] = ts(nullable=True)
    # Organización (migración 23)
    section: Mapped[str] = mapped_column(Text, default="General")
    scope: Mapped[str] = mapped_column(Text, default="contact")  # contact | deal | vehicle | appointment | flow
    pipeline: Mapped[str | None] = mapped_column(Text, nullable=True)
    maps_to: Mapped[str | None] = mapped_column(Text, nullable=True)  # llave maestra o atributo (vehicle.x, deal.x)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    show_in_card: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ContactFieldValue(Base):
    """Valor tipado de un campo personalizado (exactamente una columna value_* no nula)."""

    __tablename__ = "contact_field_values"

    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    field_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contact_fields.id", ondelete="CASCADE"), primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_number: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_bool: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source: Mapped[str] = mapped_column(Text)  # agent | ai | import | api
    updated_by: Mapped[int | None] = fk("agents.id")
    updated_at: Mapped[datetime] = ts(default=utcnow)

    field: Mapped["ContactField"] = relationship(lazy="joined")

    @property
    def value(self) -> str | int | float | bool | None:
        if self.value_number is not None:
            f = float(self.value_number)
            return int(f) if f.is_integer() else f
        if self.value_date is not None:
            return self.value_date.isoformat()
        if self.value_bool is not None:
            return self.value_bool
        return self.value_text


class ContactChange(Base):
    __tablename__ = "contact_changes"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    field_key: Mapped[str] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text)  # agent | ai | import | api | flow
    agent_id: Mapped[int | None] = fk("agents.id")
    conversation_id: Mapped[int | None] = fk("conversations.id")
    created_at: Mapped[datetime] = ts(default=utcnow)

    agent: Mapped[Agent | None] = relationship(lazy="joined")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)  # minúscula
    scope: Mapped[str] = mapped_column(Text, default="both")
    ai_description: Mapped[str | None] = mapped_column(Text, nullable=True)  # null = la IA no la usa
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class ContactTag(Base):
    __tablename__ = "contact_tags"

    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, default="agent")
    created_at: Mapped[datetime] = ts(default=utcnow)

    tag: Mapped["Tag"] = relationship(lazy="joined")


# =============================================================================
# 03 · Conversaciones
# =============================================================================
class Typification(Base):
    __tablename__ = "typifications"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_success: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)  # positive | negative | followup | neutral
    keyword: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    reactivate_bot_after_h: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    required_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = ts(default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"))
    ai_agent_id: Mapped[int | None] = fk("ai_agents.id")
    status: Mapped[str] = mapped_column(Text, default="bot")  # bot | human | closed
    assigned_agent_id: Mapped[int | None] = fk("agents.id")
    group_id: Mapped[int | None] = fk("groups.id")
    typification_id: Mapped[int | None] = fk("typifications.id")
    handoff_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    handoff_at: Mapped[datetime | None] = ts(nullable=True)
    first_response_at: Mapped[datetime | None] = ts(nullable=True)
    closed_at: Mapped[datetime | None] = ts(nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0)
    # Mantenidos por trigger desde messages (el backend solo pone unread_count = 0 al leer)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    inbound_count: Mapped[int] = mapped_column(Integer, default=0)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime] = ts(default=utcnow)
    last_inbound_at: Mapped[datetime | None] = ts(nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_ctwa_clid: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_sentiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    qa_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)  # última revisión de calidad
    recovery_attempts_sent: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_recovery_at: Mapped[datetime | None] = ts(nullable=True)
    security_flag: Mapped[str | None] = mapped_column(Text, nullable=True)  # spam | abuse | phishing
    ai_typification_id: Mapped[int | None] = fk("typifications.id")
    ai_classified_at: Mapped[datetime | None] = ts(nullable=True)
    ai_inbound_mark: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")
    channel: Mapped[Channel] = relationship(lazy="joined")
    assigned_agent: Mapped[Agent | None] = relationship(lazy="joined")
    group: Mapped[Group | None] = relationship(lazy="joined")
    typification: Mapped[Typification | None] = relationship(lazy="joined", foreign_keys=[typification_id])
    ai_typification: Mapped[Typification | None] = relationship(lazy="joined", foreign_keys=[ai_typification_id])
    tag_links: Mapped[list["ConversationTag"]] = relationship(lazy="selectin", viewonly=True)
    pending_suggestions: Mapped[list["ConversationSuggestion"]] = relationship(
        lazy="selectin", viewonly=True,
        primaryjoin="and_(Conversation.id == ConversationSuggestion.conversation_id, "
                    "ConversationSuggestion.status == 'pending')",
        order_by="ConversationSuggestion.id")


class ConversationTag(Base):
    __tablename__ = "conversation_tags"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, default="agent")  # agent | ai | rule | flow
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)

    tag: Mapped[Tag] = relationship(lazy="joined")


class ConversationSuggestion(Base):
    __tablename__ = "conversation_suggestions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # tag | typification | group | field | memory
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    value: Mapped[object] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    decided_by: Mapped[int | None] = fk("agents.id")
    decided_at: Mapped[datetime | None] = ts(nullable=True)
    ai_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class Message(Base):
    """Particionada por mes: PK (id, created_at)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(Text)  # in | out
    sender_type: Mapped[str] = mapped_column(Text)  # contact | bot | agent | campaign | flow | system
    sender_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ai_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    type: Mapped[str] = mapped_column(Text, default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="received")
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    billable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class MessageWaId(Base):
    """Idempotencia de webhooks: wa_message_id → mensaje (lo llena un trigger)."""

    __tablename__ = "message_wa_ids"

    wa_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_created_at: Mapped[datetime] = ts()
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = ts(default=utcnow)


class ConversationEvent(Base):
    """Hechos (los genera la base con triggers). Solo lectura desde el backend."""

    __tablename__ = "conversation_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text, default="system")
    actor_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# =============================================================================
# 04 · IA
# =============================================================================
class AIConnection(Base):
    __tablename__ = "ai_connections"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)  # anthropic | openai | openai_compatible | azure_openai
    model: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    default_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=60000)
    input_cost_per_mtok: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    output_cost_per_mtok: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Cortex(Base):
    __tablename__ = "cortexes"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, default="any")
    strategy: Mapped[str] = mapped_column(Text, default="failover")  # failover | lowest_latency | weighted
    max_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    circuit_breaker_failures: Mapped[int] = mapped_column(Integer, default=5)
    circuit_breaker_cooldown_s: Mapped[int] = mapped_column(Integer, default=120)
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    members: Mapped[list["CortexMember"]] = relationship(
        lazy="selectin", order_by="CortexMember.position", cascade="all, delete-orphan")


class CortexMember(Base):
    __tablename__ = "cortex_members"

    cortex_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cortexes.id", ondelete="CASCADE"), primary_key=True)
    connection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_connections.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=1)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    timeout_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    connection: Mapped[AIConnection] = relationship(lazy="joined")


class AIConnectionHealth(Base):
    __tablename__ = "ai_connection_health"

    connection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_connections.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(Text, default="closed")  # closed | open | half_open
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = ts(nullable=True)
    last_success_at: Mapped[datetime | None] = ts(nullable=True)
    last_failure_at: Mapped[datetime | None] = ts(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_p95_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class AICall(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    cortex_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    connection_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    purpose: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ai_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1)
    status: Mapped[str] = mapped_column(Text)  # ok | error | timeout | slow | invalid | refused
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_from_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AIAgent(Base):
    """Agente de IA de chat (antes "bot")."""

    __tablename__ = "ai_agents"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cortex_id: Mapped[int | None] = fk("cortexes.id")
    system_prompt: Mapped[str] = mapped_column(Text)
    handoff_message: Mapped[str] = mapped_column(Text, default="Te comunico con un asesor, en un momento te atiende.")
    use_knowledge: Mapped[bool] = mapped_column(Boolean, default=True)
    use_memory: Mapped[bool] = mapped_column(Boolean, default=True)
    use_customer_memory: Mapped[bool] = mapped_column(Boolean, default=True)
    use_catalog: Mapped[bool] = mapped_column(Boolean, default=True)
    use_appointments: Mapped[bool] = mapped_column(Boolean, default=True)
    # Configuración avanzada (migración 25, §17)
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ad_context_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ad_context_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_rules: Mapped[list] = mapped_column(JSONB, default=list)
    cost_optimization: Mapped[bool] = mapped_column(Boolean, default=True)
    security_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    security_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    security_action: Mapped[str] = mapped_column(Text, default="close")  # close | block | handoff | flag
    recovery_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_attempts: Mapped[list] = mapped_column(JSONB, default=list)  # [{after_hours, message, use_ai}] ≤ 3
    inactivity_end_hours: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    inactivity_end_typification_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    extract_field_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class AIAgentKnowledge(Base):
    __tablename__ = "ai_agent_knowledge"

    ai_agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_agents.id", ondelete="CASCADE"), primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_docs.id", ondelete="CASCADE"), primary_key=True)


class LearningRun(Base):
    __tablename__ = "learning_runs"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    type: Mapped[str] = mapped_column(Text)  # memory | seller
    status: Mapped[str] = mapped_column(Text, default="running")
    cortex_id: Mapped[int | None] = fk("cortexes.id")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    started_at: Mapped[datetime] = ts(default=utcnow)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    kind: Mapped[str] = mapped_column(Text)  # faq | objection | winning_response | fact | policy | insight
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending | approved | rejected
    source: Mapped[str] = mapped_column(Text, default="learned")  # learned | manual
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence_conversation_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    run_id: Mapped[int | None] = fk("learning_runs.id")
    reviewed_by: Mapped[int | None] = fk("agents.id")
    reviewed_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class SellerProfile(Base):
    __tablename__ = "seller_profiles"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="draft")  # draft | applied | archived
    source_agent_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    playbook: Mapped[dict] = mapped_column(JSONB)
    system_prompt: Mapped[str] = mapped_column(Text)
    ai_agent_id: Mapped[int | None] = fk("ai_agents.id")
    run_id: Mapped[int | None] = fk("learning_runs.id")
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# 05 · Catálogo y campañas
# =============================================================================
class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    sku: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    sale_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="COP")
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="manual")
    in_meta_catalog: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class CatalogSyncRun(Base):
    __tablename__ = "catalog_sync_runs"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    source: Mapped[str] = mapped_column(Text)  # import | meta | feed | api
    status: Mapped[str] = mapped_column(Text, default="running")
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    started_at: Mapped[datetime] = ts(default=utcnow)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class WaTemplate(Base):
    __tablename__ = "wa_templates"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    waba_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameter_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    components: Mapped[list] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = ts(default=utcnow)
    source: Mapped[str] = mapped_column(Text, default="meta_sync")  # meta_sync | panel | onboarding | ai
    pack_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_template_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    submitted_at: Mapped[datetime | None] = ts(nullable=True)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"))
    name: Mapped[str] = mapped_column(Text)
    template_name: Mapped[str] = mapped_column(Text)
    template_language: Mapped[str] = mapped_column(Text)
    params: Mapped[list] = mapped_column(JSONB, default=list)
    audience: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="draft")
    scheduled_at: Mapped[datetime | None] = ts(nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)
    started_at: Mapped[datetime | None] = ts(nullable=True)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"

    id: Mapped[int] = pk()
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|skipped|sent|delivered|read|failed
    wa_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = ts(nullable=True)
    delivered_at: Mapped[datetime | None] = ts(nullable=True)
    read_at: Mapped[datetime | None] = ts(nullable=True)
    failed_at: Mapped[datetime | None] = ts(nullable=True)

    contact: Mapped[Contact] = relationship(lazy="joined")


# =============================================================================
# 06 · Automatizaciones y flujos
# =============================================================================
class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Flow(Base):
    __tablename__ = "flows"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[str] = mapped_column(Text)
    trigger_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="draft")  # draft | active | paused | archived
    editor_mode: Mapped[str] = mapped_column(Text, default="junior")  # junior | advanced
    # use_alter: flows <-> flow_versions se referencian mutuamente
    current_version_id: Mapped[int | None] = fk("flow_versions.id", use_alter=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    current_version: Mapped["FlowVersion | None"] = relationship(lazy="joined", foreign_keys=[current_version_id])


class FlowVersion(Base):
    __tablename__ = "flow_versions"

    id: Mapped[int] = pk()
    flow_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)


class FlowRun(Base):
    """Particionada: PK (id, started_at)."""

    __tablename__ = "flow_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    flow_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flows.id", ondelete="CASCADE"))
    flow_version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flow_versions.id"))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trigger_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")  # running|waiting|succeeded|failed|cancelled
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_at: Mapped[datetime | None] = ts(nullable=True)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class FlowRunStep(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "flow_run_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    flow_id: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[int] = mapped_column(BigInteger)
    run_started_at: Mapped[datetime] = ts()
    block_id: Mapped[str] = mapped_column(Text)
    block_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # ok | error | skipped | waiting
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


# =============================================================================
# 07 · Operación
# =============================================================================
class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id")
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = ts()
    note: Mapped[str] = mapped_column(Text)
    done_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")
    agent: Mapped[Agent] = relationship(lazy="joined")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id")
    agent_id: Mapped[int | None] = fk("agents.id")
    starts_at: Mapped[datetime] = ts()
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    title: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="scheduled")
    created_by_type: Mapped[str] = mapped_column(Text, default="agent")  # bot | agent | flow
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")
    agent: Mapped[Agent | None] = relationship(lazy="joined")


class QuickReply(Base):
    __tablename__ = "quick_replies"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    shortcut: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    key: Mapped[str] = mapped_column(Text)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    last_sync_at: Mapped[datetime | None] = ts(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutboundWebhook(Base):
    __tablename__ = "outbound_webhooks"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    signing_secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    events: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_delivery_at: Mapped[datetime | None] = ts(nullable=True)
    source: Mapped[str] = mapped_column(Text, default="panel")  # panel | api | zapier | make | n8n
    api_key_id: Mapped[int | None] = fk("api_keys.id", ondelete="CASCADE")
    created_at: Mapped[datetime] = ts(default=utcnow)


class WebhookDelivery(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    webhook_id: Mapped[int] = mapped_column(BigInteger)
    event: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1)


class InboundEvent(Base):
    """Payload crudo de Meta. Particionada: PK (id, received_at)."""

    __tablename__ = "inbound_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="whatsapp")
    payload: Mapped[dict] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = ts(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    severity: Mapped[str] = mapped_column(Text, default="warning")
    layer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="meta")
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = ts(nullable=True)
    resolved_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# 11 · Atribución y conversiones
# =============================================================================
class TrackingSite(Base):
    __tablename__ = "tracking_sites"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text, server_default=text("replace(gen_random_uuid()::text, '-', '')"))
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    channel_id: Mapped[int | None] = fk("channels.id")
    wa_prefill: Mapped[str] = mapped_column(Text, default="Hola, quiero más información")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class WebSession(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    site_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tracking_sites.id"), nullable=True)
    link_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # visita creada por un enlace corto
    visitor_id: Mapped[str] = mapped_column(Text)
    ref_code: Mapped[str] = mapped_column(Text)
    landing_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    gclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    gbraid: Mapped[str | None] = mapped_column(Text, nullable=True)
    wbraid: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbc: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbp: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    msclkid: Mapped[str | None] = mapped_column(Text, nullable=True)
    ga_client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_click_at: Mapped[datetime | None] = ts(nullable=True)
    matched_conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched_at: Mapped[datetime | None] = ts(nullable=True)
    last_seen_at: Mapped[datetime] = ts(default=utcnow)


class WebEvent(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "web_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    session_id: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(Text)  # page_view | wa_click | custom
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


class Attribution(Base):
    __tablename__ = "attributions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    matched_by: Mapped[str] = mapped_column(Text)
    web_session_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    web_session_at: Mapped[datetime | None] = ts(nullable=True)
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    gclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    gbraid: Mapped[str | None] = mapped_column(Text, nullable=True)
    wbraid: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbc: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbp: Mapped[str | None] = mapped_column(Text, nullable=True)
    ctwa_clid: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    landing_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_id: Mapped[int | None] = fk("wa_links.id", ondelete="SET NULL")
    platform_campaign_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_campaign_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    keyword: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrichment_status: Mapped[str] = mapped_column(Text, default="skipped")  # pending | done | failed | skipped
    enriched_at: Mapped[datetime | None] = ts(nullable=True)
    touches: Mapped[int] = mapped_column(Integer, default=1)
    first_touch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_touch_at: Mapped[datetime | None] = ts(nullable=True)
    # Creativo / publicación del toque (migración 24)
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # ad | post
    page_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # "{page_id}_{post_id}"
    ad_headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class WaLink(Base):
    """Mensaje disparador: texto prellenado de WhatsApp + UTMs + anuncios + acciones al llegar (§10.5)."""

    __tablename__ = "wa_links"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    channel_id: Mapped[int | None] = fk("channels.id", ondelete="SET NULL")
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    platform: Mapped[str] = mapped_column(Text, default="web")
    trigger_text: Mapped[str] = mapped_column(Text)
    trigger_key: Mapped[str] = mapped_column(Text)
    append_ref: Mapped[bool] = mapped_column(Boolean, default=True)
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_ad_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    google_campaign_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    group_id: Mapped[int | None] = fk("groups.id", ondelete="SET NULL")
    flow_id: Mapped[int | None] = fk("flows.id", ondelete="SET NULL")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class AttributionTouch(Base):
    """Particionada: PK (id, occurred_at). Cada toque de atribución de una conversación."""

    __tablename__ = "attribution_touches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    contact_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel: Mapped[str] = mapped_column(Text)
    matched_by: Mapped[str] = mapped_column(Text)
    link_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    web_session_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    gclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    ctwa_clid: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_first: Mapped[bool] = mapped_column(Boolean, default=False)
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_headline: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdEntity(Base):
    """Caché de nombres de campañas / grupos / anuncios (Meta) y clics (gclid de Google Ads)."""

    __tablename__ = "ad_entities"
    __table_args__ = (UniqueConstraint("organization_id", "platform", "entity_type", "external_id"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    platform: Mapped[str] = mapped_column(Text)  # meta | google_ads
    entity_type: Mapped[str] = mapped_column(Text)  # ad | ad_group | campaign | click
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    keyword: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_object_story_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # publicación del anuncio
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = ts(default=utcnow)


class ConversionAction(Base):
    __tablename__ = "conversion_actions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)  # typification | stage_client | appointment_booked | deal_won
    typification_id: Mapped[int | None] = fk("typifications.id")
    value: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="COP")
    google_ads: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ConversionEvent(Base):
    __tablename__ = "conversion_events"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    action_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversion_actions.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id")
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    value: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    occurred_at: Mapped[datetime] = ts(default=utcnow)
    attribution: Mapped[dict] = mapped_column(JSONB, default=dict)
    hashed_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashed_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = ts(default=utcnow)

    action: Mapped[ConversionAction] = relationship(lazy="joined")


class ConversionUpload(Base):
    __tablename__ = "conversion_uploads"

    id: Mapped[int] = pk()
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversion_events.id", ondelete="CASCADE"))
    destination: Mapped[str] = mapped_column(Text)  # google_ads | meta_capi
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = ts(default=utcnow)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)

    event: Mapped[ConversionEvent] = relationship(lazy="joined")


# =============================================================================
# 12 · CRM
# =============================================================================
class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id")
    owner_agent_id: Mapped[int | None] = fk("agents.id")
    name: Mapped[str] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="COP")
    pipeline: Mapped[str] = mapped_column(Text, default="default")
    stage: Mapped[str] = mapped_column(Text, default="new")
    status: Mapped[str] = mapped_column(Text, default="open")  # open | won | lost
    source: Mapped[str] = mapped_column(Text, default="agent")
    expected_close: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_at: Mapped[datetime | None] = ts(nullable=True)
    lost_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK contact_vehicles (migración 23)
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)  # oportunidad generada: soat_due, trade_in…
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)  # calificación por línea (modelo, pago, plazo…)
    interest_level: Mapped[str | None] = mapped_column(Text, nullable=True)  # hot | warm | cold
    stage_changed_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")
    owner: Mapped[Agent | None] = relationship(lazy="joined")


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    provider: Mapped[str] = mapped_column(Text)  # hubspot | salesforce | google_ads | meta
    status: Mapped[str] = mapped_column(Text, default="connected")
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    instance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    refresh_token_secret_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    expires_at: Mapped[datetime | None] = ts(nullable=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = ts(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_by: Mapped[int | None] = fk("agents.id")
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class IntegrationMapping(Base):
    __tablename__ = "integration_mappings"

    id: Mapped[int] = pk()
    connection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("integration_connections.id", ondelete="CASCADE"))
    object: Mapped[str] = mapped_column(Text)  # contact | deal
    local_field: Mapped[str] = mapped_column(Text)
    remote_property: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text, default="both")  # push | pull | both
    transform: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ExternalLink(Base):
    __tablename__ = "external_links"

    id: Mapped[int] = pk()
    connection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("integration_connections.id", ondelete="CASCADE"))
    local_type: Mapped[str] = mapped_column(Text)
    local_id: Mapped[int] = mapped_column(BigInteger)
    remote_type: Mapped[str] = mapped_column(Text)
    remote_id: Mapped[str] = mapped_column(Text)
    remote_updated_at: Mapped[datetime | None] = ts(nullable=True)
    sync_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime] = ts(default=utcnow)


class IntegrationOutbox(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "integration_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"))
    connection_id: Mapped[int] = mapped_column(BigInteger)
    entity_type: Mapped[str] = mapped_column(Text)  # contact | deal | conversation_note
    entity_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(Text, default="upsert")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = ts(default=utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = ts(nullable=True)


# =============================================================================
# 13 · Voz
# =============================================================================
class VoiceAgent(Base):
    __tablename__ = "voice_agents"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_agent_id: Mapped[int | None] = fk("ai_agents.id")
    provider: Mapped[str] = mapped_column(Text, default="openai_realtime")
    model: Mapped[str] = mapped_column(Text, default="gpt-realtime")
    voice: Mapped[str] = mapped_column(Text, default="alloy")
    language: Mapped[str] = mapped_column(Text, default="es")
    greeting: Mapped[str] = mapped_column(Text, default="Hola, gracias por llamar. ¿En qué te puedo ayudar?")
    max_duration_s: Mapped[int] = mapped_column(Integer, default=600)
    transfer_group_id: Mapped[int | None] = fk("groups.id")
    record_calls: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"))
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id")
    wa_call_id: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text)  # inbound | outbound
    status: Mapped[str] = mapped_column(Text, default="ringing")
    handled_by: Mapped[str | None] = mapped_column(Text, nullable=True)  # voice_agent | agent
    voice_agent_id: Mapped[int | None] = fk("voice_agents.id")
    agent_id: Mapped[int | None] = fk("agents.id")
    started_at: Mapped[datetime] = ts(default=utcnow)
    answered_at: Mapped[datetime | None] = ts(nullable=True)
    ended_at: Mapped[datetime | None] = ts(nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, Computed(
        "case when ended_at is not null and answered_at is not null "
        "then greatest(0, extract(epoch from (ended_at - answered_at))::int) end", persisted=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")


class CallEvent(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    call_id: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class CallTurn(Base):
    __tablename__ = "call_turns"

    id: Mapped[int] = pk()
    call_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("calls.id", ondelete="CASCADE"))
    speaker: Mapped[str] = mapped_column(Text)  # contact | voice_agent | agent
    text: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# 14 · SaaS
# =============================================================================
class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = pk()
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_month_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    provider_price_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(Text, default="stripe")
    provider_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    current_period_start: Mapped[datetime | None] = ts(nullable=True)
    current_period_end: Mapped[datetime | None] = ts(nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[int] = pk()
    provider: Mapped[str] = mapped_column(Text)
    provider_event_id: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    organization_id: Mapped[int | None] = fk("organizations.id")
    payload: Mapped[dict] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = ts(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class UsageCounter(Base):
    __tablename__ = "usage_counters"

    organization_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organizations.id"), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[float] = mapped_column(Numeric, default=0)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class PlatformAdmin(Base):
    __tablename__ = "platform_admins"

    id: Mapped[int] = pk()
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# Fase 4 (migraciones 17–20): escala, omnicanal, calidad y API pública. docs/data-model.md §12
# =============================================================================
class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text)  # api | worker | all
    hostname: Mapped[str] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = ts(default=utcnow)
    last_beat_at: Mapped[datetime] = ts(default=utcnow)
    loops: Mapped[dict] = mapped_column(JSONB, default=dict)


class RealtimeSpill(Base):
    """UNLOGGED: eventos en vivo que no caben en un NOTIFY."""

    __tablename__ = "realtime_spill"

    id: Mapped[int] = pk()
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = ts(default=utcnow)


class RateLimitCounter(Base):
    """UNLOGGED: usar public.rate_limit_hit(bucket, window_s)."""

    __tablename__ = "rate_limit_counters"

    bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)


class ContactIdentity(Base):
    __tablename__ = "contact_identities"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)  # whatsapp_cloud | messenger | instagram | webchat
    channel_id: Mapped[int | None] = fk("channels.id", ondelete="CASCADE")
    external_id: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)  # WhatsApp: teléfono cuando Meta lo envía
    bsuid: Mapped[str | None] = mapped_column(Text, nullable=True)  # WhatsApp: user_id (BSUID)
    parent_bsuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_inbound_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class WebchatSession(Base):
    __tablename__ = "webchat_sessions"
    __table_args__ = (UniqueConstraint("channel_id", "visitor_id"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id", ondelete="CASCADE"))
    visitor_id: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text)
    contact_id: Mapped[int | None] = fk("contacts.id", ondelete="SET NULL")
    conversation_id: Mapped[int | None] = fk("conversations.id", ondelete="SET NULL")
    web_session_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    last_seen_at: Mapped[datetime] = ts(default=utcnow)


class QAScorecard(Base):
    __tablename__ = "qa_scorecards"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    applies_to: Mapped[str] = mapped_column(Text, default="agent")  # agent | bot | any
    criteria: Mapped[list] = mapped_column(JSONB)  # [{key, label, description, weight, critical}]
    auto_review: Mapped[bool] = mapped_column(Boolean, default=True)
    sample_pct: Mapped[int] = mapped_column(Integer, default=100)
    min_messages: Mapped[int] = mapped_column(Integer, default=3)
    group_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ConversationReview(Base):
    __tablename__ = "conversation_reviews"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"))
    scorecard_id: Mapped[int | None] = fk("qa_scorecards.id", ondelete="SET NULL")
    reviewer_type: Mapped[str] = mapped_column(Text)  # ai | human
    reviewer_agent_id: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    subject_type: Mapped[str] = mapped_column(Text)  # agent | bot
    agent_id: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    ai_agent_id: Mapped[int | None] = fk("ai_agents.id", ondelete="SET NULL")
    status: Mapped[str] = mapped_column(Text, default="done")  # pending | done | failed | disputed
    scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    total_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    critical_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    sentiment: Mapped[str | None] = mapped_column(Text, nullable=True)  # positive | neutral | negative | mixed
    sentiment_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    customer_effort: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispute_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class CoachingItem(Base):
    __tablename__ = "coaching_items"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="CASCADE"))
    review_id: Mapped[int | None] = fk("conversation_reviews.id", ondelete="SET NULL")
    criterion_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="open")  # open | acknowledged | done | dismissed
    created_at: Mapped[datetime] = ts(default=utcnow)
    resolved_at: Mapped[datetime | None] = ts(nullable=True)


class AgentTestSuite(Base):
    __tablename__ = "agent_test_suites"
    __table_args__ = (UniqueConstraint("ai_agent_id", "name"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    ai_agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ai_agents.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_on_change: Mapped[bool] = mapped_column(Boolean, default=True)
    min_pass_pct: Mapped[int] = mapped_column(Integer, default=80)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class AgentTestCase(Base):
    __tablename__ = "agent_test_cases"

    id: Mapped[int] = pk()
    suite_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_test_suites.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    turns: Mapped[list] = mapped_column(JSONB)  # [{role: user|assistant, text}]
    expectations: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = ts(default=utcnow)


class AgentTestRun(Base):
    __tablename__ = "agent_test_runs"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    suite_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_test_suites.id", ondelete="CASCADE"))
    ai_agent_id: Mapped[int] = mapped_column(BigInteger)
    config_revision_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trigger: Mapped[str] = mapped_column(Text, default="manual")  # manual | on_change | schedule
    status: Mapped[str] = mapped_column(Text, default="running")  # running | passed | failed | error
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    pass_pct: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    started_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    started_at: Mapped[datetime] = ts(default=utcnow)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class AgentTestResult(Base):
    __tablename__ = "agent_test_results"

    id: Mapped[int] = pk()
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_test_runs.id", ondelete="CASCADE"))
    case_id: Mapped[int | None] = fk("agent_test_cases.id", ondelete="SET NULL")
    passed: Mapped[bool] = mapped_column(Boolean)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks: Mapped[list] = mapped_column(JSONB, default=list)
    judge_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_call_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    created_at: Mapped[datetime] = ts(default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=120)
    last_used_at: Mapped[datetime | None] = ts(nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = ts(default=utcnow)


class ApiRequest(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "api_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    api_key_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    method: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[int] = mapped_column(SmallInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"

    organization_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agents.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferences: Mapped[dict] = mapped_column(JSONB, default=lambda: {"assigned": True, "message_assigned": True,
                                                                       "call": True})
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# Onboarding y configuración automática (migración 21). docs/data-model.md §13
# =============================================================================
class OnboardingRun(Base):
    __tablename__ = "onboarding_runs"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    status: Mapped[str] = mapped_column(Text, default="in_progress")  # in_progress | completed | abandoned
    current_step: Mapped[str] = mapped_column(Text, default="company")
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    answers: Mapped[dict] = mapped_column(JSONB, default=dict)
    channel_id: Mapped[int | None] = fk("channels.id", ondelete="SET NULL")
    started_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    started_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)
    completed_at: Mapped[datetime | None] = ts(nullable=True)


class OnboardingStep(Base):
    __tablename__ = "onboarding_steps"
    __table_args__ = (UniqueConstraint("run_id", "key"),)

    id: Mapped[int] = pk()
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("onboarding_runs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|running|done|warning|failed|skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = ts(nullable=True)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class ChannelHealthCheck(Base):
    __tablename__ = "channel_health_checks"

    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id", ondelete="CASCADE"),
                                            primary_key=True)
    check_key: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[int] = org_fk()
    status: Mapped[str] = mapped_column(Text)  # pass | warn | fail | pending | skipped
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    fixable: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = ts(default=utcnow)


class AgentInvitation(Base):
    __tablename__ = "agent_invitations"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, default="agent")
    group_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = ts()
    accepted_at: Mapped[datetime | None] = ts(nullable=True)
    accepted_agent_id: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    revoked_at: Mapped[datetime | None] = ts(nullable=True)
    invited_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    email_sent_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class InteractionProduct(Base):
    """Producto mencionado / cotizado / comprado en una conversación (catálogo o referencia externa). §14"""

    __tablename__ = "interaction_products"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = fk("conversations.id", ondelete="SET NULL")
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    product_id: Mapped[int | None] = fk("products.id", ondelete="SET NULL")
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(Text, default="interested")  # mentioned|interested|quoted|purchased|not_interested
    quantity: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    source: Mapped[str] = mapped_column(Text)  # ai|agent|flow|api|whatsapp_order|catalog_message|import
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    created_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# Registro maestro del cliente (migración 23). docs/data-model.md §15
# =============================================================================
class GoldenKeyType(Base):
    __tablename__ = "golden_key_types"

    id: Mapped[int] = pk()
    organization_id: Mapped[int | None] = fk("organizations.id", ondelete="CASCADE")  # null = del sistema
    key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    normalizer: Mapped[str] = mapped_column(Text)  # phone|email|username|document|plate|vin|name|date|address|text
    is_identifier: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    multi: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_extract: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    archived_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class ContactKey(Base):
    __tablename__ = "contact_keys"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    key_type: Mapped[str] = mapped_column(Text)
    subtype: Mapped[str | None] = mapped_column(Text, nullable=True)
    value: Mapped[str] = mapped_column(Text)
    value_normalized: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    rank: Mapped[str] = mapped_column(Text, default="secondary")  # primary | secondary
    status: Mapped[str] = mapped_column(Text, default="active")  # active | superseded | rejected
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    extraction_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = ts(default=utcnow)
    last_seen_at: Mapped[datetime] = ts(default=utcnow)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ContactVehicle(Base):
    __tablename__ = "contact_vehicles"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    plate: Mapped[str | None] = mapped_column(Text, nullable=True)
    vin: Mapped[str | None] = mapped_column(Text, nullable=True)
    make: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    fuel: Mapped[str | None] = mapped_column(Text, nullable=True)
    mileage_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    relation: Mapped[str] = mapped_column(Text, default="owner")
    status: Mapped[str] = mapped_column(Text, default="active")
    insurance_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    inspection_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_service_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    extraction_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class KeyExtraction(Base):
    __tablename__ = "key_extractions"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_kind: Mapped[str] = mapped_column(Text)  # conversation | document | image | audio
    document_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    extracted: Mapped[dict] = mapped_column(JSONB, default=dict)
    keys_added: Mapped[int] = mapped_column(Integer, default=0)
    keys_updated: Mapped[int] = mapped_column(Integer, default=0)
    ai_call_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    finished_at: Mapped[datetime | None] = ts(nullable=True)


class ContactMergeCandidate(Base):
    __tablename__ = "contact_merge_candidates"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_a_id: Mapped[int] = mapped_column(BigInteger)  # FK en la base (dos FK a contacts: sin relación ORM)
    contact_b_id: Mapped[int] = mapped_column(BigInteger)
    matched: Mapped[list] = mapped_column(JSONB)
    score: Mapped[float] = mapped_column(REAL)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending | merged | dismissed
    decided_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    decided_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class ContactGolden(Base):
    """Registro agregado (lo mantiene un trigger sobre contact_keys): solo lectura para la app."""

    __tablename__ = "contact_golden"

    contact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # FK en la base
    organization_id: Mapped[int] = mapped_column(BigInteger)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    phones: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    primary_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    emails: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    usernames: Mapped[list] = mapped_column(JSONB, default=list)
    document_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    birthdate: Mapped[date | None] = mapped_column(Date, nullable=True)
    addresses: Mapped[list] = mapped_column(JSONB, default=list)
    plates: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    vins: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    completeness_pct: Mapped[int] = mapped_column(SmallInteger, default=0)
    keys_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class ContactConsent(Base):
    __tablename__ = "contact_consents"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    contact_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="CASCADE"))
    consent_type: Mapped[str] = mapped_column(Text)  # habeas_data | terms | marketing | data_sharing | call_recording
    granted: Mapped[bool] = mapped_column(Boolean)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    recorded_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    recorded_at: Mapped[datetime] = ts(default=utcnow)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class AdSpendDaily(Base):
    """Inversión por anuncio / grupo / campaña y día (Meta Insights, Google Ads). §16"""

    __tablename__ = "ad_spend_daily"

    organization_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, primary_key=True)  # meta | google_ads
    level: Mapped[str] = mapped_column(Text, primary_key=True)  # ad | ad_group | campaign
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    spend: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    platform_conversations: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fetched_at: Mapped[datetime] = ts(default=utcnow)


# =============================================================================
# Agente avanzado, etapas y webhooks entrantes (migración 25). docs/data-model.md §17
# =============================================================================
class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    __table_args__ = (UniqueConstraint("organization_id", "pipeline", "key"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    pipeline: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    external_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    probability: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    is_won: Mapped[bool] = mapped_column(Boolean, default=False)
    is_lost: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts(default=utcnow)


class DealStageEvent(Base):
    __tablename__ = "deal_stage_events"

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    deal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("deals.id", ondelete="CASCADE"))
    from_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_stage: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)  # ai | agent | flow | crm | api | rule | webhook
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    agent_id: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    created_at: Mapped[datetime] = ts(default=utcnow)


class InboundWebhook(Base):
    __tablename__ = "inbound_webhooks"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[int] = pk()
    organization_id: Mapped[int] = org_fk()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    token_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")  # draft | active | paused
    action: Mapped[str] = mapped_column(Text)  # send_template|send_text|start_flow|upsert_contact|create_deal|create_appointment
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # FK en la base
    template_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    flow_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    params: Mapped[list] = mapped_column(JSONB, default=list)
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    executions: Mapped[int] = mapped_column(BigInteger, default=0)
    succeeded: Mapped[int] = mapped_column(BigInteger, default=0)
    failed: Mapped[int] = mapped_column(BigInteger, default=0)
    last_run_at: Mapped[datetime | None] = ts(nullable=True)
    created_by: Mapped[int | None] = fk("agents.id", ondelete="SET NULL")
    published_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts(default=utcnow)
    updated_at: Mapped[datetime] = ts(default=utcnow)


class InboundWebhookRun(Base):
    """Particionada: PK (id, created_at)."""

    __tablename__ = "inbound_webhook_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utcnow)
    organization_id: Mapped[int] = mapped_column(BigInteger)
    webhook_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)  # succeeded | failed | rejected | duplicate
    http_status: Mapped[int] = mapped_column(SmallInteger)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
