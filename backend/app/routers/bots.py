"""Agentes de IA (ruta /api/bots por compatibilidad): CRUD, Cortex, recursos conectados, conocimiento y canales."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, AIAgent, AIAgentKnowledge, Channel, Cortex, KnowledgeDoc
from app.schemas import BotOut, BotUpdate
from app.settings_store import add_revision
from app.plans import enforce_limit

router = APIRouter(prefix="/api/bots", tags=["ai-agents"])

EDITABLE = ("name", "description", "enabled", "cortex_id", "system_prompt", "handoff_message", "use_knowledge",
            "use_memory", "use_customer_memory", "use_catalog", "use_appointments",
            # Configuración avanzada (§17)
            "timezone", "max_words", "ad_context_enabled", "ad_context_prompt", "source_rules", "cost_optimization",
            "security_enabled", "security_prompt", "security_action", "recovery_enabled", "recovery_attempts",
            "inactivity_end_hours", "inactivity_end_typification_id", "extract_field_ids")
NULLABLE = {"timezone", "max_words", "ad_context_prompt", "security_prompt", "inactivity_end_hours",
            "inactivity_end_typification_id"}


class BotCreate(BaseModel):
    name: str
    system_prompt: str
    description: str | None = None
    enabled: bool = True
    cortex_id: int | None = None
    handoff_message: str | None = None
    use_knowledge: bool = True
    use_memory: bool = True
    use_customer_memory: bool = True
    use_catalog: bool = True
    use_appointments: bool = True
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


class IdsIn(BaseModel):
    ids: list[int]


async def bot_out(session: AsyncSession, bot: AIAgent) -> BotOut:
    channel_ids = list((await session.scalars(
        select(Channel.id).where(Channel.default_ai_agent_id == bot.id).order_by(Channel.id))).all())
    return BotOut(id=bot.id, name=bot.name, description=bot.description, enabled=bot.enabled,
                  cortex_id=bot.cortex_id, system_prompt=bot.system_prompt, handoff_message=bot.handoff_message,
                  use_knowledge=bot.use_knowledge, use_memory=bot.use_memory,
                  use_customer_memory=bot.use_customer_memory, use_catalog=bot.use_catalog,
                  use_appointments=bot.use_appointments, channel_ids=channel_ids,
                  **{k: getattr(bot, k) for k in EDITABLE[11:]})


def bot_document(out: BotOut) -> dict:
    """Documento JSON editable (sin id ni canales, que tienen su propio endpoint)."""
    return {k: getattr(out, k) for k in EDITABLE}


async def get_bot(session: AsyncSession, bot_id: int, org: int) -> AIAgent:
    bot = await session.get(AIAgent, bot_id)
    if not bot or bot.organization_id != org:
        raise HTTPException(404, "Agente no encontrado")
    return bot


async def validate_bot_changes(session: AsyncSession, org: int, changes: dict, bot_id: int | None = None) -> None:
    if "name" in changes:
        if not (changes["name"] or "").strip():
            raise HTTPException(422, "El nombre es obligatorio")
        dup = await session.scalar(select(AIAgent.id).where(AIAgent.organization_id == org,
                                                            AIAgent.name == changes["name"].strip()))
        if dup and dup != bot_id:
            raise HTTPException(409, "Ya existe un agente con ese nombre")
        changes["name"] = changes["name"].strip()
    if "system_prompt" in changes and not (changes["system_prompt"] or "").strip():
        raise HTTPException(422, "El prompt es obligatorio")
    if changes.get("cortex_id") is not None:
        cx = await session.get(Cortex, changes["cortex_id"])
        if not cx or cx.organization_id != org:
            raise HTTPException(422, "Cortex inválido")
    if "handoff_message" in changes and not changes["handoff_message"]:
        changes.pop("handoff_message")
    await validate_advanced(session, org, changes)


async def validate_advanced(session: AsyncSession, org: int, changes: dict) -> None:
    """Valida la configuración avanzada (reglas por fuente, recuperación, seguridad, campos, zona horaria)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from pydantic import ValidationError
    from sqlalchemy import func

    from app.agent_config import validate_attempts, validate_rules
    from app.models import ContactField, Typification

    for key in [k for k, v in changes.items() if v is None and k not in NULLABLE and k in EDITABLE[11:]]:
        changes.pop(key)  # null en un campo no anulable = no cambiar
    try:
        if "source_rules" in changes:
            changes["source_rules"] = validate_rules(changes["source_rules"])
        if "recovery_attempts" in changes:
            changes["recovery_attempts"] = validate_attempts(changes["recovery_attempts"])
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, f"Configuración inválida: {e}") from e
    if changes.get("timezone"):
        try:
            ZoneInfo(changes["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise HTTPException(422, "Zona horaria inválida") from e
    if changes.get("security_action") and changes["security_action"] not in ("close", "block", "handoff", "flag"):
        raise HTTPException(422, "Acción de seguridad inválida")
    if changes.get("max_words") is not None and not 20 <= changes["max_words"] <= 2000:
        raise HTTPException(422, "El máximo de palabras debe estar entre 20 y 2000")
    if changes.get("inactivity_end_hours") is not None and not 0 < changes["inactivity_end_hours"] <= 72:
        raise HTTPException(422, "El fin por inactividad debe estar entre 0 y 72 horas")
    if changes.get("inactivity_end_typification_id") is not None:
        t = await session.get(Typification, changes["inactivity_end_typification_id"])
        if not t or t.organization_id != org:
            raise HTTPException(422, "Tipificación inválida")
    if changes.get("extract_field_ids"):
        ids = set(changes["extract_field_ids"])
        found = await session.scalar(select(func.count()).where(ContactField.organization_id == org,
                                                                ContactField.id.in_(ids)))
        if found != len(ids):
            raise HTTPException(422, "Hay campos que no existen")


async def apply_bot_update(session: AsyncSession, bot: AIAgent, changes: dict, agent_id: int | None,
                           source: str = "human", ai_prompt: str | None = None) -> BotOut:
    await validate_bot_changes(session, bot.organization_id, changes, bot.id)
    for k, v in changes.items():
        if k in EDITABLE:
            setattr(bot, k, v)
    await session.flush()
    out = await bot_out(session, bot)
    await add_revision(session, bot.organization_id, "ai_agent", bot.id, bot_document(out), source, agent_id,
                       ai_prompt=ai_prompt)
    await session.commit()
    __import__("app.quality.hooks", fromlist=["x"]).on_agent_changed(bot.organization_id, bot.id)  # pruebas QA
    return out


@router.get("", response_model=list[BotOut])
async def list_bots(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    bots = (await session.scalars(select(AIAgent).where(AIAgent.organization_id == agent.organization_id)
                                  .order_by(AIAgent.id))).all()
    return [await bot_out(session, b) for b in bots]


@router.get("/{bot_id}", response_model=BotOut)
async def get_one(bot_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await bot_out(session, await get_bot(session, bot_id, agent.organization_id))


@router.post("", response_model=BotOut)
async def create_bot(body: BotCreate, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    data = body.model_dump()
    await validate_bot_changes(session, agent.organization_id, data)
    await enforce_limit(session, agent.organization_id, "ai_agents")
    bot = AIAgent(organization_id=agent.organization_id, **data)
    session.add(bot)
    await session.flush()
    out = await bot_out(session, bot)
    await add_revision(session, agent.organization_id, "ai_agent", bot.id, bot_document(out), "human", agent.id)
    await session.commit()
    return out


@router.put("/{bot_id}", response_model=BotOut)
async def update_bot(bot_id: int, body: BotUpdate, agent: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    bot = await get_bot(session, bot_id, agent.organization_id)
    return await apply_bot_update(session, bot, body.model_dump(exclude_unset=True), agent.id)


@router.delete("/{bot_id}")
async def delete_bot(bot_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    bot = await get_bot(session, bot_id, agent.organization_id)
    if await session.scalar(select(Channel.id).where(Channel.default_ai_agent_id == bot.id)):
        raise HTTPException(409, "El agente atiende un número de WhatsApp: asigna otro agente al canal primero")
    await session.delete(bot)  # las conversaciones quedan con ai_agent_id = null (FK on delete set null)
    await session.commit()
    return {"ok": True}


@router.get("/{bot_id}/knowledge")
async def get_knowledge_links(bot_id: int, agent: Agent = Depends(current_agent),
                              session: AsyncSession = Depends(get_session)):
    await get_bot(session, bot_id, agent.organization_id)
    ids = (await session.scalars(select(AIAgentKnowledge.doc_id).where(AIAgentKnowledge.ai_agent_id == bot_id))).all()
    return {"doc_ids": sorted(ids)}


@router.put("/{bot_id}/knowledge")
async def set_knowledge_links(bot_id: int, body: IdsIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    await get_bot(session, bot_id, agent.organization_id)
    valid = set((await session.scalars(select(KnowledgeDoc.id).where(
        KnowledgeDoc.organization_id == agent.organization_id, KnowledgeDoc.id.in_(body.ids or [0])))).all())
    if set(body.ids) - valid:
        raise HTTPException(422, "Hay documentos que no existen")
    await session.execute(delete(AIAgentKnowledge).where(AIAgentKnowledge.ai_agent_id == bot_id))
    for doc_id in sorted(valid):
        session.add(AIAgentKnowledge(ai_agent_id=bot_id, doc_id=doc_id))
    await session.commit()
    return {"doc_ids": sorted(valid)}


@router.put("/{bot_id}/channels", response_model=BotOut)
async def set_channels(bot_id: int, body: IdsIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """El agente pasa a atender esos números (cada número tiene un solo agente por defecto)."""
    bot = await get_bot(session, bot_id, agent.organization_id)
    channels = (await session.scalars(select(Channel).where(
        Channel.organization_id == agent.organization_id, Channel.id.in_(body.ids or [0])))).all()
    if len(channels) != len(set(body.ids)):
        raise HTTPException(422, "Hay canales que no existen")
    await session.execute(update(Channel).where(Channel.default_ai_agent_id == bot.id,
                                                Channel.id.not_in(body.ids or [0])).values(default_ai_agent_id=None))
    for ch in channels:
        ch.default_ai_agent_id = bot.id
    await session.commit()
    return await bot_out(session, bot)
