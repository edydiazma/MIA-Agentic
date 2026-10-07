"""Clasificador de conversaciones con IA (vía Cortex, propósito "classification").

Con cada análisis el modelo devuelve (JSON con esquema estricto): resumen, sentimiento, etiquetas del
catálogo, tipificación, grupo destino, valores para la ficha del cliente y la memoria del cliente.
Cada resultado se aplica, queda como sugerencia (fila en conversation_suggestions) o se ignora según la
configuración. La IA nunca sobrescribe un dato que editó una persona.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import CallContext, complete_json, resolve_cortex
from app.ai.structured import LLMError
from app.db import SessionLocal, set_actor
from app.fields import coerce, custom_values, human_edited_keys, set_custom, set_native
from app.models import (
    Agent,
    ContactField,
    Conversation,
    ConversationSuggestion,
    Group,
    Message,
    Product,
    Typification,
    utcnow,
)
from app.settings_store import get_setting

log = logging.getLogger(__name__)

TRIGGERS = ("manual", "handoff", "close", "periodic", "test")
MODES = {"tags": ("auto", "suggest", "off"), "group": ("auto", "suggest", "off"),
         "typification": ("auto", "suggest", "off"), "fields": ("fill_empty", "overwrite_ai", "suggest", "off"),
         "memory": ("auto", "off")}
SENDER = {"contact": "Cliente", "bot": "Bot", "agent": "Asesor", "campaign": "Campaña", "flow": "Flujo"}

_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_tasks: set[asyncio.Task] = set()


class ClassifierDisabled(Exception):
    pass


@dataclass
class Catalog:
    tags: list[dict]
    typifications: list[Typification]
    groups: list[Group]
    fields: dict[str, ContactField]
    products: list = None  # candidatos del catálogo encontrados en el texto del cliente (Cliente 360)
    golden: dict = None  # tipos de llave del registro maestro a extraer (misma llamada, §15)

    @property
    def typification_names(self) -> list[str]:
        return [t.name for t in self.typifications]


def validate_classifier(body: dict) -> None:
    """Valida un PUT de la configuración del clasificador (lanza HTTPException 422). Normaliza etiquetas."""
    if body.get("cortex_id") not in (None, "") and not str(body["cortex_id"]).isdigit():
        raise HTTPException(422, "cortex_id inválido")
    for k, v in (body.get("modes") or {}).items():
        if k not in MODES or v not in MODES[k]:
            raise HTTPException(422, f"Modo inválido para {k}")
    if "min_confidence" in body and not 0 <= float(body["min_confidence"]) <= 1:
        raise HTTPException(422, "La confianza mínima va de 0 a 1")
    if "every_n_messages" in body and int(body["every_n_messages"]) < 0:
        raise HTTPException(422, "every_n_messages no puede ser negativo")
    for t in body.get("tags") or []:  # las etiquetas se guardan en minúscula
        t["name"] = (t.get("name") or "").strip().lower()
    names = [t["name"] for t in body.get("tags") or []]
    if any(not n for n in names) or len(names) != len(set(names)):
        raise HTTPException(422, "Cada etiqueta necesita un nombre único")


async def load_catalog(session: AsyncSession, org: int, cfg: dict) -> Catalog:
    typs = list((await session.scalars(
        select(Typification).where(Typification.organization_id == org, Typification.is_active)
        .order_by(Typification.position, Typification.id))).all())
    groups = list((await session.scalars(select(Group).where(Group.organization_id == org).order_by(Group.name))).all())
    fields = {f.key: f for f in (await session.scalars(
        select(ContactField).where(ContactField.organization_id == org, ContactField.ai_extract,
                                   ContactField.archived_at.is_(None))
        .order_by(ContactField.position, ContactField.id))).all()}
    tags = [t for t in cfg.get("tags", []) if t.get("name")]
    golden = None
    if (await get_setting(session, "golden", org))["extract_conversations"]:
        from app.golden.keys import key_types

        golden = {k: t for k, t in (await key_types(session, org)).items() if t.ai_extract} or None
    return Catalog(tags, typs, groups, fields, golden=golden)


PRODUCT_STAGES = ("mentioned", "interested", "quoted", "purchased", "not_interested")
MAX_PRODUCT_CANDIDATES = 8


async def product_candidates(session: AsyncSession, org: int, msgs: list[Message]) -> list:
    """Productos del catálogo que aparecen en los últimos mensajes del cliente (búsqueda por mensaje)."""
    from app import catalog

    if not await session.scalar(select(func.count()).select_from(Product).where(Product.organization_id == org)):
        return []
    texts = [m.text or m.transcript for m in msgs if m.direction == "in" and (m.text or m.transcript)][-10:]
    joined = " ".join(texts).lower()[:4000]
    # 1) nombres o SKU que aparecen tal cual en lo que escribió el cliente
    found: dict[int, object] = {p.id: p for p in (await session.scalars(
        select(Product).where(Product.organization_id == org,
                              (func.strpos(joined, func.lower(Product.name)) > 0)
                              | (func.strpos(joined, func.lower(Product.sku)) > 0))
        .order_by(func.length(Product.name).desc()).limit(MAX_PRODUCT_CANDIDATES))).all()}
    # 2) búsqueda aproximada por mensaje (modelos parciales, errores de tipeo)
    for m in reversed([m for m in msgs if m.direction == "in" and (m.text or m.transcript)][-10:]):
        for p in await catalog.search(session, org, (m.text or m.transcript)[:200], limit=3):
            found.setdefault(p.id, p)
        if len(found) >= MAX_PRODUCT_CANDIDATES:
            break
    return list(found.values())[:MAX_PRODUCT_CANDIDATES]


# --- Prompt y esquema -----------------------------------------------------------
def build_schema(cat: Catalog, modes: dict) -> dict:
    props: dict = {
        "summary": {"type": "string", "description": "Resumen de 1 a 3 frases de la conversación"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "reason": {"type": "string", "description": "Justificación breve de la clasificación"},
    }
    if cat.tags and modes.get("tags") != "off":
        props["tags"] = {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": [t["name"] for t in cat.tags]},
                           "confidence": {"type": "number"}},
            "required": ["name", "confidence"], "additionalProperties": False}}
    if cat.typifications and modes.get("typification") != "off":
        props["typification"] = {"type": "string", "enum": [*cat.typification_names, ""]}
        props["typification_confidence"] = {"type": "number"}
    if cat.groups and modes.get("group") != "off":
        props["group"] = {"type": "string", "enum": [g.name for g in cat.groups] + [""]}
        props["group_confidence"] = {"type": "number"}
    if cat.fields and modes.get("fields") != "off":
        props["fields"] = {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string", "enum": list(cat.fields)},
                           "value": {"type": "string"},
                           "confidence": {"type": "number"},
                           "evidence": {"type": "string", "description": "Frase del cliente que lo respalda"}},
            "required": ["key", "value", "confidence", "evidence"], "additionalProperties": False}}
    # Productos de la conversación (Cliente 360): misma llamada, sin costo adicional de IA
    props["products"] = {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "catalog_sku_or_null": {"type": "string",
                                               "description": "SKU del catálogo si coincide; \"\" si no está"},
                       "stage": {"type": "string", "enum": list(PRODUCT_STAGES)},
                       "confidence": {"type": "number"}},
        "required": ["name", "catalog_sku_or_null", "stage", "confidence"], "additionalProperties": False}}
    if cat.golden:  # registro maestro: llaves, vehículos y consentimientos en la misma llamada
        from app.golden.extract import conversation_schema

        props.update(conversation_schema(cat.golden))
    if modes.get("memory", "auto") != "off":
        props["customer_memory"] = {
            "type": "string",
            "description": "Memoria del cliente reescrita integrando la actual y lo nuevo de esta conversación"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def build_system(cfg: dict, cat: Catalog) -> str:
    """Parte estable del prompt (se cachea): instrucciones del negocio + catálogos."""
    parts = [cfg.get("instructions") or "", "",
             "Devuelve el análisis en el esquema JSON indicado. Las confianzas van de 0 a 1. "
             "Usa \"\" cuando no aplique una tipificación o un grupo."]
    if cat.tags:
        parts += ["", "## Etiquetas (elige todas las que apliquen, ninguna si no hay evidencia)"]
        parts += [f"- {t['name']}: {t.get('description') or ''}" for t in cat.tags]
    if cat.typifications:
        parts += ["", "## Tipificaciones (elige la que mejor describa el resultado de la conversación)"]
        parts += [f"- {t.name}" + (f": {t.criteria}" if t.criteria else "") for t in cat.typifications]
    if cat.groups:
        parts += ["", "## Grupos de asesores (elige el que debería atender al cliente)"]
        parts += [f"- {g.name}" + (f": {g.description}" if g.description else "") for g in cat.groups]
    if cfg["modes"].get("memory", "auto") != "off":
        parts += ["", "## Memoria del cliente",
                  "Reescribe la memoria del cliente integrando la memoria actual con lo nuevo de esta conversación: "
                  "datos estables, preferencias, productos de interés, presupuesto, objeciones, compromisos y "
                  "próximos pasos. Conserva lo que siga vigente (incluido lo que escribieron los asesores), corrige "
                  "lo que cambió, máximo 12 viñetas cortas que empiecen con «- ». No incluyas datos sensibles "
                  "(documentos, tarjetas, contraseñas). Si no hay nada nuevo, devuelve la memoria actual igual."]
    parts += ["", "## Productos",
              "Lista los productos o servicios concretos que el cliente mencionó, por los que preguntó, que se le "
              "cotizaron o que compró en esta conversación (etapas: mentioned, interested, quoted, purchased, "
              "not_interested). Si coincide con un producto del catálogo de abajo usa su SKU; si no, deja "
              "catalog_sku_or_null vacío y escribe el nombre como lo dijo el cliente. No inventes productos."]
    if cat.products:
        parts += ["Catálogo (candidatos encontrados en la conversación):"]
        parts += [f"- {p.sku}: {p.name}" for p in cat.products]
    if cat.golden:
        from app.golden.extract import conversation_instructions

        parts += ["", conversation_instructions(cat.golden)]
    if cat.fields:
        parts += ["", "## Datos del cliente a extraer (solo si el cliente los dijo; formato según el tipo)"]
        for f in cat.fields.values():
            fmt = {"number": "número", "date": "fecha AAAA-MM-DD", "boolean": "sí/no", "email": "email",
                   "phone": "teléfono", "select": "una de: " + ", ".join(f.options or [])}.get(f.type, "texto")
            parts.append(f"- {f.key} ({f.label}, {fmt})" + (f": {f.description}" if f.description else ""))
    return "\n".join(parts).strip()


def render_message(m: Message, agent_names: dict[int, str]) -> str | None:
    if m.sender_type == "system":
        return None
    who = SENDER.get(m.sender_type, m.sender_type)
    if m.sender_type == "agent" and m.sender_agent_id in agent_names:
        who = f"Asesor {agent_names[m.sender_agent_id]}"
    body = {"audio": f"[Nota de voz] {m.transcript or '(sin transcribir)'}", "image": "[Imagen]",
            "sticker": "[Sticker]", "video": "[Video]",
            "document": f"[Documento {m.media_filename or ''}]"}.get(m.type, "")
    text = " ".join(x for x in (body, m.text or "") if x).strip() or f"[{m.type}]"
    return f"{who}: {text}"


def build_user(conv: Conversation, msgs: list[Message], cat: Catalog, agent_names: dict[int, str]) -> str:
    current = custom_values(conv.contact)
    lines = [f"Cliente: {conv.contact.name or 'desconocido'}"]
    if conv.ad_headline:
        lines.append(f"Llegó desde el anuncio: «{conv.ad_headline}»")
    if conv.group:
        lines.append(f"Grupo actual: {conv.group.name}")
    known = {f.label: current[k] for k, f in cat.fields.items() if current.get(k) not in (None, "")}
    if known:
        lines.append("Datos ya registrados: " + "; ".join(f"{k}={v}" for k, v in known.items()))
    if conv.contact.memory:
        lines += ["Memoria actual del cliente:", conv.contact.memory]
    lines += ["", "<conversacion>"]
    lines += [t for t in (render_message(m, agent_names) for m in msgs) if t]
    lines.append("</conversacion>")
    return "\n".join(lines)


# --- Normalización y aplicación -------------------------------------------------
def _conf(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def sanitize(raw: dict, cat: Catalog) -> dict:
    """Descarta valores fuera de catálogo (algunos endpoints compatibles no respetan el esquema)."""
    tag_names = {t["name"] for t in cat.tags}
    groups = {g.name for g in cat.groups}
    typs = set(cat.typification_names)
    out = {
        "summary": str(raw.get("summary") or "")[:1000],
        "sentiment": raw.get("sentiment") if raw.get("sentiment") in ("positive", "neutral", "negative") else None,
        "reason": str(raw.get("reason") or "")[:1000],
        "tags": [{"name": t["name"], "confidence": _conf(t.get("confidence"))}
                 for t in raw.get("tags") or [] if isinstance(t, dict) and t.get("name") in tag_names],
        "typification": raw.get("typification") if raw.get("typification") in typs else None,
        "typification_confidence": _conf(raw.get("typification_confidence")),
        "group": raw.get("group") if raw.get("group") in groups else None,
        "group_confidence": _conf(raw.get("group_confidence")),
        "fields": [],
        "customer_memory": str(raw.get("customer_memory") or "").strip()[:3000] or None,
        "products": [{"name": str(p.get("name") or "").strip()[:200],
                      "catalog_sku_or_null": str(p.get("catalog_sku_or_null") or "").strip() or None,
                      "stage": p.get("stage") if p.get("stage") in PRODUCT_STAGES else "interested",
                      "confidence": _conf(p.get("confidence"))}
                     for p in raw.get("products") or [] if isinstance(p, dict) and str(p.get("name") or "").strip()],
    }
    if cat.golden:
        from app.golden.extract import sanitize_conversation

        out["golden"] = sanitize_conversation(raw, cat.golden)
    for f in raw.get("fields") or []:
        if isinstance(f, dict) and f.get("key") in cat.fields:
            out["fields"].append({"key": f["key"], "value": f.get("value"), "confidence": _conf(f.get("confidence")),
                                  "evidence": str(f.get("evidence") or "")[:300]})
    return out


async def suggest(session: AsyncSession, conv: Conversation, kind: str, target: str | None, value,
                  confidence: float | None, evidence: str | None = None) -> None:
    """Reemplaza la sugerencia pendiente del mismo tipo/objetivo por una nueva."""
    cond = [ConversationSuggestion.conversation_id == conv.id, ConversationSuggestion.kind == kind,
            ConversationSuggestion.status == "pending"]
    cond.append(ConversationSuggestion.target.is_(None) if target is None else ConversationSuggestion.target == target)
    await session.execute(update(ConversationSuggestion).where(*cond).values(status="expired"))
    session.add(ConversationSuggestion(organization_id=conv.organization_id, conversation_id=conv.id, kind=kind,
                                       target=target, value=value, confidence=confidence, evidence=evidence))


async def apply_result(session: AsyncSession, conv: Conversation, cfg: dict, cat: Catalog, res: dict,
                       trigger: str, ai_call_id: int | None = None) -> dict:
    from app import service  # import diferido: service importa este módulo

    await set_actor(session, "ai")
    modes, threshold = cfg["modes"], float(cfg["min_confidence"])
    applied: dict = {"tags": [], "group": None, "typification": None, "fields": {}, "memory": False}
    notes: list[str] = []

    conv.ai_summary = res["summary"] or conv.ai_summary
    conv.ai_sentiment = res["sentiment"]
    conv.ai_classified_at = utcnow()

    # Etiquetas
    current_tags = {link.tag.name for link in conv.tag_links}
    strong = {t["name"]: t["confidence"] for t in res["tags"] if t["confidence"] >= threshold}
    new_tags = [n for n in strong if n not in current_tags]
    if modes.get("tags") == "auto" and new_tags:
        applied["tags"] = await service.set_conversation_tags(session, conv, new_tags, "ai", confidence=strong,
                                                              replace=False)
    elif modes.get("tags") == "suggest":
        for name in new_tags:
            await suggest(session, conv, "tag", name, name, strong[name])

    # Tipificación: se registra siempre lo que dijo la IA para medir su acierto
    typ = next((t for t in cat.typifications if t.name == res["typification"]), None)
    if typ:
        conv.ai_typification_id = typ.id
        strong_typ = res["typification_confidence"] >= threshold
        if (trigger == "close" and conv.status == "closed" and not conv.typification_id
                and modes.get("typification") == "auto" and strong_typ):
            conv.typification_id = typ.id
            applied["typification"] = typ.name
            notes.append(f"tipificada como «{typ.name}»")
        elif modes.get("typification") != "off" and conv.status != "closed" and conv.typification_id != typ.id:
            await suggest(session, conv, "typification", None, typ.name, res["typification_confidence"])

    # Grupo
    group = next((g for g in cat.groups if g.name == res["group"]), None)
    if group and group.id != conv.group_id and modes.get("group") != "off":
        routable = trigger == "handoff" or (conv.status == "human" and not conv.assigned_agent_id)
        if modes.get("group") == "auto" and routable and res["group_confidence"] >= threshold:
            conv.group_id = group.id
            applied["group"] = group.name
            notes.append(f"enviada al grupo «{group.name}»")
        elif conv.status != "closed":
            await suggest(session, conv, "group", str(group.id), {"group_id": group.id, "name": group.name},
                          res["group_confidence"])

    # Campos de la ficha
    contact = conv.contact
    if res["fields"] and modes.get("fields") != "off":
        protected = await human_edited_keys(session, contact.id)
        current = custom_values(contact)
        for item in res["fields"]:
            f = cat.fields[item["key"]]
            try:
                value = coerce(f, item["value"])
            except ValueError:
                continue
            if value is None or value == current.get(f.key):
                continue
            can_write = (item["confidence"] >= threshold and f.key not in protected
                         and (modes["fields"] == "overwrite_ai" or current.get(f.key) in (None, "")))
            if modes["fields"] in ("fill_empty", "overwrite_ai") and can_write:
                await set_custom(session, contact, f, value, "ai", conversation_id=conv.id)
                applied["fields"][f.key] = value
            else:
                await suggest(session, conv, "field", f.key, {"value": value, "label": f.label},
                              item["confidence"], item["evidence"])

    # Memoria del cliente
    memory = res.get("customer_memory")
    if modes.get("memory", "auto") == "auto" and memory and memory != (contact.memory or "").strip():
        set_native(session, contact, "memory", memory, "ai", conversation_id=conv.id)
        applied["memory"] = True

    # Productos por interacción (confianza ≥ 0.6)
    if trigger != "test" and res.get("products"):
        from app.interaction_products import record_ai_products

        applied["products"] = await record_ai_products(session, conv, res["products"])

    # Registro maestro: llaves de identificación, vehículos y consentimientos
    if trigger != "test" and res.get("golden"):
        from app.golden.extract import apply_conversation

        ext = await apply_conversation(session, conv, res["golden"], ai_call_id)
        if ext:
            applied["golden"] = {"keys_added": ext.keys_added, "keys_updated": ext.keys_updated}

    if notes:
        await service.system_note(session, conv,
                                  "IA: " + "; ".join(notes) + (f". {res['reason']}" if res["reason"] else ""))
    return applied


# --- Ejecución ------------------------------------------------------------------
async def classify(session: AsyncSession, conv: Conversation, trigger: str, apply: bool = True,
                   cfg_override: dict | None = None) -> dict:
    org = conv.organization_id
    cfg = await get_setting(session, "classifier", org)
    if cfg_override:
        override = {k: v for k, v in cfg_override.items() if k in cfg}
        if isinstance(override.get("modes"), dict):
            override["modes"] = {**cfg["modes"], **override["modes"]}
        cfg = {**cfg, **override}
    if not cfg["enabled"] and trigger != "test":
        raise ClassifierDisabled("La clasificación con IA está desactivada")

    cat = await load_catalog(session, org, cfg)
    rows = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc()).limit(int(cfg["max_messages"])))).all()
    msgs = list(reversed(rows))
    if not any(m.direction == "in" for m in msgs):
        return {"skipped": "La conversación no tiene mensajes del cliente", "applied": None}
    cat.products = await product_candidates(session, org, msgs)

    agent_names = dict((await session.execute(
        select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
    ctx = CallContext(organization_id=org, purpose="test" if trigger == "test" else "classification",
                      conversation_id=conv.id)
    cx = await resolve_cortex(session, org, cfg.get("cortex_id"), "classification")
    started = time.monotonic()
    raw = await complete_json(session, cx, build_system(cfg, cat), build_user(conv, msgs, cat, agent_names),
                              build_schema(cat, cfg["modes"]), ctx)
    result = sanitize(raw, cat)
    out = {"result": result, "latency_ms": int((time.monotonic() - started) * 1000), "applied": None,
           "attempts": ctx.attempts, "cortex": cx.name}
    if apply:
        from app import service

        out["applied"] = await apply_result(session, conv, cfg, cat, result, trigger,
                                            ctx.call_ids[-1] if ctx.call_ids else None)
        conv.ai_inbound_mark = conv.inbound_count
        await service.commit_and_broadcast(session, conv)
    return out


async def run_in_background(conversation_id: int, trigger: str) -> None:
    async def job():
        async with _locks[conversation_id], SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if not conv:
                return
            try:
                await classify(session, conv, trigger)
            except ClassifierDisabled:
                pass
            except LLMError as e:
                log.warning("Clasificación fallida (conv %s, %s): %s", conversation_id, trigger, e)
            except Exception:
                log.exception("Error clasificando la conversación %s", conversation_id)

    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def maybe_periodic(session: AsyncSession, conv: Conversation) -> None:
    """Re-analiza cada N mensajes del cliente (inbound_count lo mantiene un trigger)."""
    cfg = await get_setting(session, "classifier", conv.organization_id)
    n = int(cfg.get("every_n_messages") or 0)
    if not cfg["enabled"] or n <= 0:
        return
    inbound = await session.scalar(select(Conversation.inbound_count).where(Conversation.id == conv.id)) or 0
    if inbound - (conv.ai_inbound_mark or 0) >= n:
        await run_in_background(conv.id, "periodic")


async def route_on_handoff(session: AsyncSession, conv: Conversation) -> None:
    """Antes de asignar asesor: deja que la IA elija el grupo (con tiempo límite)."""
    cfg = await get_setting(session, "classifier", conv.organization_id)
    if not (cfg["enabled"] and cfg["route_on_handoff"] and cfg["modes"].get("group") != "off"):
        return
    try:
        async with _locks[conv.id]:
            await asyncio.wait_for(classify(session, conv, "handoff"), timeout=25)
    except (TimeoutError, LLMError, ClassifierDisabled) as e:
        log.warning("No se pudo enrutar con IA la conversación %s: %s", conv.id, e)
        await session.rollback()
        await session.refresh(conv)


async def pending_count(session: AsyncSession, conv_id: int) -> int:
    return await session.scalar(select(func.count()).where(
        ConversationSuggestion.conversation_id == conv_id, ConversationSuggestion.status == "pending")) or 0
