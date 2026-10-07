"""Clasificación con IA, sugerencias, etiquetas de conversación y campos personalizados del cliente."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import service
from app.ai.structured import LLMError
from app.auth import current_agent, require_admin
from app.classifier import ClassifierDisabled, classify, validate_classifier
from app.db import get_session, set_actor
from app.fields import FIELD_TYPES, KEY_RE, NATIVE_FIELDS, coerce, set_custom
from app.models import (
    Agent,
    Contact,
    ContactChange,
    ContactField,
    Conversation,
    ConversationSuggestion,
    ConversationTag,
    Group,
    Tag,
    utcnow,
)
from app.schemas import UTCDateTime
from app.settings_store import get_setting

router = APIRouter(prefix="/api", tags=["classifier"])


class ClassifyIn(BaseModel):
    apply: bool = True


class TestIn(BaseModel):
    conversation_id: int
    config: dict | None = None  # configuración sin guardar, para probar antes de guardar


class SuggestionIn(BaseModel):
    kind: str  # tags | typification | group | field
    key: str | None = None  # clave del campo (kind=field) o etiqueta (kind=tags, opcional)


class TagsIn(BaseModel):
    tags: list[str]


class FieldIn(BaseModel):
    key: str
    label: str
    type: str = "text"
    options: list[str] | None = None
    description: str | None = None
    ai_extract: bool = True
    agent_editable: bool = True
    position: int = 100
    # Organización (registro maestro, §15): None = no cambia
    section: str | None = None
    scope: str | None = None  # contact | deal | vehicle | appointment | flow
    pipeline: str | None = None  # línea de negocio si scope = deal
    maps_to: str | None = None  # llave maestra (document, plate…) o atributo (vehicle.mileage_km, consent.x, deal.x)
    aliases: list[str] | None = None
    show_in_card: bool | None = None
    currency: str | None = None


ORG_KEYS = ("section", "scope", "pipeline", "maps_to", "aliases", "show_in_card", "currency")


class FieldOut(FieldIn):
    id: int


class ChangeOut(BaseModel):
    id: int
    field_key: str
    label: str
    old_value: str | None
    new_value: str | None
    source: str
    agent_name: str | None
    conversation_id: int | None
    created_at: UTCDateTime


async def _conv(session: AsyncSession, conv_id: int, agent: Agent) -> Conversation:
    conv = await service.get_conversation(session, conv_id, agent.organization_id)
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")
    return conv


async def _run(session: AsyncSession, conv: Conversation, trigger: str, apply: bool, override: dict | None = None):
    try:
        return await classify(session, conv, trigger, apply=apply, cfg_override=override)
    except ClassifierDisabled as e:
        raise HTTPException(409, str(e)) from None
    except LLMError as e:
        raise HTTPException(502, f"El modelo no respondió correctamente: {e}") from None


# --- Clasificación --------------------------------------------------------------
@router.post("/classifier/test")
async def test_classifier(body: TestIn, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Prueba la configuración (guardada o la del formulario) sin aplicar nada."""
    if body.config:
        validate_classifier(dict(body.config))
    conv = await _conv(session, body.conversation_id, agent)
    return await _run(session, conv, "test", apply=False, override=body.config)


@router.post("/conversations/{conv_id}/classify")
async def classify_conversation(conv_id: int, body: ClassifyIn | None = None, agent: Agent = Depends(current_agent),
                                session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, conv_id, agent)
    out = await _run(session, conv, "manual", apply=body.apply if body else True)
    await service.reload(session, conv)
    return {**out, "conversation": service.conversation_out(conv)}


async def _decide(session: AsyncSession, rows: list[ConversationSuggestion], status: str, agent: Agent) -> None:
    for s in rows:
        s.status, s.decided_by, s.decided_at = status, agent.id, utcnow()


@router.post("/conversations/{conv_id}/suggestions/{action}")
async def handle_suggestion(conv_id: int, action: str, body: SuggestionIn, agent: Agent = Depends(current_agent),
                            session: AsyncSession = Depends(get_session)):
    """Acepta o descarta una sugerencia de la IA (contrato estable: responde la conversación)."""
    if action not in ("accept", "dismiss"):
        raise HTTPException(404, "Acción desconocida")
    conv = await _conv(session, conv_id, agent)
    kind = {"tags": "tag", "field": "field", "typification": "typification", "group": "group"}.get(body.kind)
    if not kind:
        raise HTTPException(422, "Tipo de sugerencia inválido")
    stmt = select(ConversationSuggestion).where(
        ConversationSuggestion.conversation_id == conv.id, ConversationSuggestion.kind == kind,
        ConversationSuggestion.status == "pending")
    if kind == "field":
        stmt = stmt.where(ConversationSuggestion.target == body.key)
    elif kind == "tag" and body.key:
        stmt = stmt.where(ConversationSuggestion.target == body.key.strip().lower())
    rows = list((await session.scalars(stmt)).all())
    if not rows:
        raise HTTPException(404, "No hay una sugerencia pendiente de ese tipo")

    await set_actor(session, "agent", agent.id)
    if action == "accept":
        if kind == "tag":
            await service.set_conversation_tags(session, conv, [r.value for r in rows], "agent", agent_id=agent.id,
                                                replace=False)
        elif kind == "typification":
            typ = await service.typification_by_name(session, conv.organization_id, rows[-1].value)
            if typ:
                conv.typification_id = typ.id
        elif kind == "group":
            gid = rows[-1].value.get("group_id")
            group = await session.get(Group, gid)
            if group and group.organization_id == conv.organization_id:
                conv.group_id = gid
        elif kind == "field":
            field = await session.scalar(select(ContactField).where(
                ContactField.organization_id == conv.organization_id, ContactField.key == rows[-1].target))
            if field:
                # Aceptada por una persona: queda protegida frente a cambios posteriores de la IA.
                await set_custom(session, conv.contact, field, coerce(field, rows[-1].value.get("value")), "agent",
                                 agent_id=agent.id, conversation_id=conv.id)
    await _decide(session, rows, "accepted" if action == "accept" else "dismissed", agent)
    await service.commit_and_broadcast(session, conv)
    return service.conversation_out(conv)


# --- Etiquetas de conversación --------------------------------------------------
@router.get("/conversation-tags")
async def conversation_tags(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Catálogo de la organización (con criterio para la IA) y uso en conversaciones."""
    counts = dict((await session.execute(
        select(ConversationTag.tag_id, func.count()).join(Tag, Tag.id == ConversationTag.tag_id)
        .where(Tag.organization_id == agent.organization_id).group_by(ConversationTag.tag_id))).all())
    tags = (await session.scalars(select(Tag).where(Tag.organization_id == agent.organization_id,
                                                    Tag.scope.in_(["conversation", "both"])).order_by(Tag.name))).all()
    return [{"name": t.name, "description": t.ai_description or "", "in_catalog": t.ai_description is not None,
             "count": counts.get(t.id, 0)} for t in tags]


@router.put("/conversations/{conv_id}/tags")
async def set_conversation_tags(conv_id: int, body: TagsIn, agent: Agent = Depends(current_agent),
                                session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, conv_id, agent)
    await set_actor(session, "agent", agent.id)
    await service.set_conversation_tags(session, conv, body.tags, "agent", agent_id=agent.id, replace=True)
    # Las sugerencias de etiquetas que la persona ya resolvió dejan de estar pendientes
    await session.execute(update(ConversationSuggestion).where(
        ConversationSuggestion.conversation_id == conv.id, ConversationSuggestion.kind == "tag",
        ConversationSuggestion.status == "pending").values(status="dismissed", decided_by=agent.id,
                                                           decided_at=utcnow()))
    await service.commit_and_broadcast(session, conv)
    return service.conversation_out(conv)


# --- Campos personalizados ------------------------------------------------------
def _validate_field(body: FieldIn) -> None:
    if not KEY_RE.match(body.key):
        raise HTTPException(422, "La clave debe empezar con letra y usar solo minúsculas, números y _")
    if body.key in NATIVE_FIELDS:
        raise HTTPException(422, "Esa clave está reservada")
    if body.type not in FIELD_TYPES:
        raise HTTPException(422, f"Tipo inválido: {', '.join(FIELD_TYPES)}")
    if body.type == "select" and not [o for o in body.options or [] if o.strip()]:
        raise HTTPException(422, "Una lista necesita opciones")
    if not body.label.strip():
        raise HTTPException(422, "El nombre visible es obligatorio")
    from app.golden.fields import validate_organization

    try:
        validate_organization(body.scope, body.pipeline, body.maps_to)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def _field_out(f: ContactField) -> FieldOut:
    return FieldOut(id=f.id, key=f.key, label=f.label, type=f.type, options=f.options, description=f.description,
                    ai_extract=f.ai_extract, agent_editable=f.agent_editable, position=f.position, section=f.section,
                    scope=f.scope, pipeline=f.pipeline, maps_to=f.maps_to, aliases=list(f.aliases or []),
                    show_in_card=f.show_in_card, currency=f.currency)


def _field_values(body: FieldIn) -> dict:
    """Campos del cuerpo a guardar (los de organización solo si vienen)."""
    data = body.model_dump(exclude=set(ORG_KEYS))
    for k in ORG_KEYS:
        v = getattr(body, k)
        if v is not None:
            data[k] = [a.strip() for a in v if a and a.strip()] if k == "aliases" else v
    if data.get("scope") and data["scope"] != "deal":
        data.setdefault("pipeline", None)
    return data


@router.get("/contact-fields", response_model=list[FieldOut])
async def list_fields(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(ContactField).where(
        ContactField.organization_id == agent.organization_id, ContactField.archived_at.is_(None))
        .order_by(ContactField.position, ContactField.id))).all()
    return [_field_out(f) for f in rows]


@router.post("/contact-fields", response_model=FieldOut)
async def create_field(body: FieldIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    _validate_field(body)
    existing = await session.scalar(select(ContactField).where(
        ContactField.organization_id == agent.organization_id, ContactField.key == body.key))
    options = [o.strip() for o in body.options or [] if o.strip()] or None
    if existing and existing.archived_at is None:
        raise HTTPException(409, "Ya existe un campo con esa clave")
    if existing:  # se reactiva un campo archivado (conserva sus valores)
        f = existing
        for k, v in _field_values(body).items():
            setattr(f, k, v)
        f.options, f.archived_at = options, None
    else:
        f = ContactField(organization_id=agent.organization_id, **{**_field_values(body), "options": options})
        session.add(f)
    await session.commit()
    return _field_out(f)


async def _field(session: AsyncSession, fid: int, agent: Agent) -> ContactField:
    f = await session.get(ContactField, fid)
    if not f or f.organization_id != agent.organization_id or f.archived_at is not None:
        raise HTTPException(404, "Campo no encontrado")
    return f


@router.put("/contact-fields/{fid}", response_model=FieldOut)
async def update_field(fid: int, body: FieldIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    f = await _field(session, fid, agent)
    _validate_field(body)
    if body.key != f.key:
        raise HTTPException(422, "La clave no se puede cambiar (los valores guardados dependen de ella)")
    if body.type != f.type:
        raise HTTPException(422, "El tipo no se puede cambiar: crea un campo nuevo")
    for k, v in _field_values(body).items():
        setattr(f, k, v)
    f.options = [o.strip() for o in body.options or [] if o.strip()] or None
    if f.scope == "deal" and not f.pipeline:
        raise HTTPException(422, "Un campo de oportunidad necesita la línea de negocio (pipeline)")
    await session.commit()
    return _field_out(f)


@router.delete("/contact-fields/{fid}")
async def delete_field(fid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Archiva la definición; los valores y su historial se conservan."""
    f = await _field(session, fid, agent)
    f.archived_at = utcnow()
    await session.commit()
    return {"ok": True}


@router.get("/contacts/{contact_id}/history", response_model=list[ChangeOut])
async def field_history(contact_id: int, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    contact = await session.get(Contact, contact_id)
    if not contact or contact.organization_id != agent.organization_id:
        raise HTTPException(404, "Contacto no encontrado")
    labels = {**NATIVE_FIELDS, **{f.key: f.label for f in (await session.scalars(
        select(ContactField).where(ContactField.organization_id == agent.organization_id))).all()}}
    rows = (await session.scalars(select(ContactChange).where(ContactChange.contact_id == contact_id)
                                  .order_by(ContactChange.id.desc()).limit(200))).unique().all()
    return [ChangeOut(id=c.id, field_key=c.field_key, label=labels.get(c.field_key, c.field_key),
                      old_value=c.old_value, new_value=c.new_value, source=c.source,
                      agent_name=c.agent.name if c.agent else None, conversation_id=c.conversation_id,
                      created_at=c.created_at) for c in rows]


@router.get("/classifier/stats")
async def classifier_stats(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Tasa de aceptación de sugerencias por tipo (para afinar la configuración)."""
    rows = (await session.execute(
        select(ConversationSuggestion.kind, ConversationSuggestion.status, func.count())
        .where(ConversationSuggestion.organization_id == agent.organization_id)
        .group_by(ConversationSuggestion.kind, ConversationSuggestion.status))).all()
    out: dict[str, dict] = {}
    for kind, status, n in rows:
        out.setdefault(kind, {})[status] = n
    for v in out.values():
        decided = v.get("accepted", 0) + v.get("dismissed", 0)
        v["acceptance_pct"] = round(100 * v.get("accepted", 0) / decided, 1) if decided else None
    cfg = await get_setting(session, "classifier", agent.organization_id)
    return {"enabled": cfg["enabled"], "by_kind": out}
