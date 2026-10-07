"""Configuración como documento JSON (estilo n8n): exportar/importar y editar con IA en lenguaje natural.

Flujo de edición con IA: propuesta (el LLM devuelve el documento completo modificado) → validación contra el
JSON Schema de la entidad + diff → el administrador aplica → se guarda por la ruta normal de la entidad y queda
una revisión (config_revisions) con source="ai" y la instrucción usada.
"""

import json

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import CallContext, complete_json, resolve_cortex
from app.ai.structured import LLMError
from app.auth import require_admin
from app.classifier import validate_classifier
from app.db import get_session
from app.json_schemas import ENTITY_TYPES, AIAgentDoc, AutomationDoc, CortexDoc, schema_for
from app.models import Agent, Automation, ConfigRevision, Cortex, Flow, FlowVersion
from app.routers.bots import apply_bot_update, bot_document, bot_out, get_bot
from app.routers.cortex import CortexIn, apply_cortex, cortex_document, cortex_out
from app.schemas import UTCDateTime
from app.settings_store import DEFAULTS, add_revision, get_setting, set_setting

router = APIRouter(prefix="/api", tags=["json-edit"])

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_json": {"type": "string", "description": "El documento COMPLETO modificado, serializado como JSON"},
        "summary": {"type": "string", "description": "Qué cambiaste, en una o dos frases"},
    },
    "required": ["document_json", "summary"],
    "additionalProperties": False,
}


class ProposeIn(BaseModel):
    entity_type: str
    entity_id: str
    instruction: str
    cortex_id: int | None = None


class DocumentIn(BaseModel):
    entity_type: str
    entity_id: str
    document: dict
    instruction: str | None = None  # obligatoria al aplicar una propuesta de la IA


class RevisionOut(BaseModel):
    id: int
    entity_type: str
    entity_id: str
    revision: int
    source: str
    ai_prompt: str | None
    ai_call_id: int | None
    created_by: int | None
    created_at: UTCDateTime
    document: dict | None = None


# --- Carga y guardado por entidad -----------------------------------------------
def _setting_key(entity_type: str, entity_id: str) -> str:
    key = "classifier" if entity_type == "classifier" else entity_id
    if key not in DEFAULTS:
        raise HTTPException(404, "Configuración desconocida")
    return key


async def load_document(session: AsyncSession, org: int, entity_type: str, entity_id: str) -> dict:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(422, f"Tipo de entidad inválido: {', '.join(ENTITY_TYPES)}")
    if entity_type in ("setting", "classifier"):
        return await get_setting(session, _setting_key(entity_type, entity_id), org)
    if entity_type == "ai_agent":
        return bot_document(await bot_out(session, await get_bot(session, int(entity_id), org)))
    if entity_type == "cortex":
        cx = await session.get(Cortex, int(entity_id))
        if not cx or cx.organization_id != org:
            raise HTTPException(404, "Cortex no encontrado")
        return cortex_document(await cortex_out(session, cx))
    if entity_type == "automation":
        a = await _automation(session, org, entity_id)
        return {"name": a.name, "type": a.type, "config": a.config or {}, "enabled": a.enabled, "priority": a.priority}
    flow = await _flow(session, org, entity_id)
    if flow.current_version_id:
        version = await session.get(FlowVersion, flow.current_version_id)
        return version.definition
    return {"schema_version": 1, "variables": {}, "scripts": []}


async def _automation(session: AsyncSession, org: int, entity_id: str) -> Automation:
    a = await session.get(Automation, int(entity_id))
    if not a or a.organization_id != org:
        raise HTTPException(404, "Automatización no encontrada")
    return a


async def _flow(session: AsyncSession, org: int, entity_id: str) -> Flow:
    f = await session.get(Flow, int(entity_id))
    if not f or f.organization_id != org:
        raise HTTPException(404, "Flujo no encontrado")
    return f


def validate_document(entity_type: str, entity_id: str, doc: dict) -> list[str]:
    schema = schema_for(entity_type, entity_id if entity_type == "setting" else None)
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.absolute_path)) or '(raíz)'}: {e.message}"
            for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))]


async def save_document(session: AsyncSession, agent: Agent, entity_type: str, entity_id: str, doc: dict,
                        source: str, instruction: str | None = None, ai_call_id: int | None = None) -> dict:
    """Guarda por la ruta normal de cada entidad (mismas validaciones) y deja la revisión."""
    org = agent.organization_id
    errors = validate_document(entity_type, entity_id, doc)
    if errors:
        raise HTTPException(422, {"message": "El documento no cumple el esquema", "errors": errors})

    if entity_type in ("setting", "classifier"):
        key = _setting_key(entity_type, entity_id)
        doc = {k: v for k, v in doc.items() if k != "last_sync"}
        if key == "classifier":
            validate_classifier(doc)
        saved = await set_setting(session, key, doc, org=org, agent_id=agent.id, source=source)
        if instruction or ai_call_id:  # set_setting ya creó la revisión: se completa con la instrucción
            rev = await session.scalar(select(ConfigRevision).where(
                ConfigRevision.organization_id == org, ConfigRevision.entity_type == "setting",
                ConfigRevision.entity_id == key).order_by(ConfigRevision.revision.desc()).limit(1))
            rev.ai_prompt, rev.ai_call_id = instruction, ai_call_id
            await session.commit()
        return saved

    if entity_type == "ai_agent":
        try:
            data = AIAgentDoc(**doc).model_dump()
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        bot = await get_bot(session, int(entity_id), org)
        out = await apply_bot_update(session, bot, data, agent.id, source=source, ai_prompt=instruction)
        return bot_document(out)

    if entity_type == "cortex":
        try:
            body = CortexIn(**CortexDoc(**doc).model_dump())
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        cx = await session.get(Cortex, int(entity_id))
        if not cx or cx.organization_id != org:
            raise HTTPException(404, "Cortex no encontrado")
        await apply_cortex(session, cx, body, org)
        document = cortex_document(await cortex_out(session, cx))
        await add_revision(session, org, "cortex", cx.id, document, source, agent.id, instruction, ai_call_id)
        await session.commit()
        return document

    if entity_type == "automation":
        try:
            data = AutomationDoc(**doc).model_dump()
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        a = await _automation(session, org, entity_id)
        for k, v in data.items():
            setattr(a, k, v)
        await add_revision(session, org, "automation", a.id, data, source, agent.id, instruction, ai_call_id)
        await session.commit()
        return data

    # flow: cada guardado es una versión nueva e inmutable que pasa a ser la actual
    flow = await _flow(session, org, entity_id)
    last = await session.scalar(select(func.max(FlowVersion.version)).where(FlowVersion.flow_id == flow.id)) or 0
    version = FlowVersion(flow_id=flow.id, version=last + 1, definition=doc,
                          schema_version=int(doc.get("schema_version") or 1), created_by_ai=source == "ai",
                          ai_prompt=instruction, ai_call_id=ai_call_id, created_by=agent.id,
                          change_note=(instruction or "")[:500] or None)
    session.add(version)
    await session.flush()
    flow.current_version_id = version.id
    await add_revision(session, org, "flow", flow.id, doc, source, agent.id, instruction, ai_call_id)
    await session.commit()
    return doc


def diff(before, after, path: str = "") -> list[dict]:
    """Diferencias entre dos documentos JSON (rutas tipo a/b/0/c)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after), key=str):
            p = f"{path}/{k}" if path else str(k)
            if k not in before:
                out.append({"path": p, "before": None, "after": after[k], "change": "added"})
            elif k not in after:
                out.append({"path": p, "before": before[k], "after": None, "change": "removed"})
            else:
                out += diff(before[k], after[k], p)
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (b, a) in enumerate(zip(before, after, strict=True)):
            out += diff(b, a, f"{path}/{i}" if path else str(i))
        return out
    return [] if before == after else [{"path": path or "(raíz)", "before": before, "after": after,
                                        "change": "changed"}]


# --- Endpoints ------------------------------------------------------------------
@router.post("/ai/json-edit")
async def propose_edit(body: ProposeIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """La IA propone el documento modificado; no se guarda nada hasta aplicar."""
    if not body.instruction.strip():
        raise HTTPException(422, "Escribe qué quieres cambiar")
    current = await load_document(session, agent.organization_id, body.entity_type, body.entity_id)
    schema = schema_for(body.entity_type, body.entity_id if body.entity_type == "setting" else None)
    system = (
        "Editas documentos JSON de configuración de una plataforma de atención por WhatsApp con agentes de IA. "
        "Aplica SOLO el cambio pedido, conserva todo lo demás igual (mismas claves, mismos valores) y devuelve el "
        "documento COMPLETO en `document_json`. El documento debe cumplir este JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    user = (f"Instrucción: {body.instruction.strip()}\n\nDocumento actual ({body.entity_type} {body.entity_id}):\n"
            + json.dumps(current, ensure_ascii=False, indent=2, default=str))
    ctx = CallContext(organization_id=agent.organization_id, purpose="json_edit")
    try:
        cx = await resolve_cortex(session, agent.organization_id, body.cortex_id, "json_edit")
        out = await complete_json(session, cx, system, user, EDIT_SCHEMA, ctx, max_tokens=8000)
    except LLMError as e:
        raise HTTPException(502, f"El modelo no respondió correctamente: {e}") from None
    call_ids = list(ctx.call_ids)  # exactamente las llamadas de esta edición
    try:
        proposal = json.loads(out["document_json"])
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(502, "La IA devolvió un JSON inválido; reformula la instrucción") from None
    if not isinstance(proposal, dict):
        raise HTTPException(502, "La IA no devolvió un objeto JSON")
    errors = validate_document(body.entity_type, body.entity_id, proposal)
    return {"entity_type": body.entity_type, "entity_id": body.entity_id, "current": current, "proposal": proposal,
            "summary": out.get("summary", ""), "diff": diff(current, proposal), "valid": not errors,
            "errors": errors, "ai_call_ids": call_ids, "attempts": ctx.attempts}


@router.post("/ai/json-edit/apply")
async def apply_edit(body: DocumentIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if not (body.instruction or "").strip():
        raise HTTPException(422, "Falta la instrucción con la que la IA generó el documento")
    before = await load_document(session, agent.organization_id, body.entity_type, body.entity_id)
    saved = await save_document(session, agent, body.entity_type, body.entity_id, body.document, "ai",
                                body.instruction.strip())
    return {"document": saved, "diff": diff(before, saved)}


@router.get("/ai/json-edit/document")
async def export_document(entity_type: str, entity_id: str, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    return {"entity_type": entity_type, "entity_id": entity_id,
            "document": await load_document(session, agent.organization_id, entity_type, entity_id),
            "schema": schema_for(entity_type, entity_id if entity_type == "setting" else None)}


@router.put("/ai/json-edit/document")
async def import_document(body: DocumentIn, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Importa/edita el documento a mano (mismas validaciones; revisión con source="import")."""
    before = await load_document(session, agent.organization_id, body.entity_type, body.entity_id)
    saved = await save_document(session, agent, body.entity_type, body.entity_id, body.document, "import")
    return {"document": saved, "diff": diff(before, saved)}


def _rev(r: ConfigRevision, with_doc: bool = False) -> RevisionOut:
    return RevisionOut(id=r.id, entity_type=r.entity_type, entity_id=r.entity_id, revision=r.revision, source=r.source,
                       ai_prompt=r.ai_prompt, ai_call_id=r.ai_call_id, created_by=r.created_by,
                       created_at=r.created_at, document=r.document if with_doc else None)


@router.get("/revisions", response_model=list[RevisionOut])
async def list_revisions(entity_type: str, entity_id: str, limit: int = Query(default=50, le=200),
                         agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    stored_type = "setting" if entity_type == "classifier" else entity_type
    stored_id = "classifier" if entity_type == "classifier" else entity_id
    rows = (await session.scalars(select(ConfigRevision).where(
        ConfigRevision.organization_id == agent.organization_id, ConfigRevision.entity_type == stored_type,
        ConfigRevision.entity_id == stored_id).order_by(ConfigRevision.revision.desc()).limit(limit))).all()
    return [_rev(r) for r in rows]


@router.get("/revisions/{rev_id}", response_model=RevisionOut)
async def get_revision(rev_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    r = await session.get(ConfigRevision, rev_id)
    if not r or r.organization_id != agent.organization_id:
        raise HTTPException(404, "Revisión no encontrada")
    return _rev(r, with_doc=True)


@router.post("/revisions/{rev_id}/restore")
async def restore_revision(rev_id: int, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    """Vuelve a aplicar el documento de una revisión anterior (queda como revisión nueva)."""
    r = await session.get(ConfigRevision, rev_id)
    if not r or r.organization_id != agent.organization_id:
        raise HTTPException(404, "Revisión no encontrada")
    if r.entity_type not in ENTITY_TYPES:
        raise HTTPException(422, "Esta revisión no se puede restaurar desde aquí")
    saved = await save_document(session, agent, r.entity_type, r.entity_id, r.document, "human",
                                f"Restaurada la revisión {r.revision}")
    return {"document": saved}

