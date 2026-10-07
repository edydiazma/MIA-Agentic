"""Sugerencias de respuesta, siguiente mejor acción, borradores, reescritura, resúmenes y resultados. §19.3

Costo: una llamada por ráfaga de mensajes del cliente (respuestas + siguiente acción en el mismo JSON), una por
traspaso (resumen), una por cierre (resumen) y una por cada borrador / reescritura que pida el asesor.
"""

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.copilot import guard, llm
from app.copilot.context import CopilotContext, build, history, transcript_of
from app.models import Agent, Conversation, CopilotSuggestion, utcnow
from app.settings_store import get_setting

log = logging.getLogger(__name__)

ACTIONS = ("send_product", "offer_appointment", "send_template", "ask_field", "handoff", "create_deal", "move_stage",
           "none")
NULLABLE = {"type": ["string", "null"]}
PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {k: NULLABLE for k in ("text", "sku", "product_name", "field", "pipeline", "stage", "template",
                                          "group", "date")},
    "required": ["text", "sku", "product_name", "field", "pipeline", "stage", "template", "group", "date"],
    "additionalProperties": False,
}
NEXT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "label": {"type": "string"},
        "payload": PAYLOAD_SCHEMA,
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "label", "payload", "reason", "confidence"],
    "additionalProperties": False,
}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
            "additionalProperties": False}},
        "next_action": NEXT_ACTION_SCHEMA,
    },
    "required": ["suggestions", "next_action"],
    "additionalProperties": False,
}
TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
               "additionalProperties": False}
HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string"},
        "captured_data": {"type": "array", "items": {"type": "string"}},
        "bot_promises": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]},
        "next_step": {"type": "string"},
    },
    "required": ["need", "captured_data", "bot_promises", "sentiment", "next_step"],
    "additionalProperties": False,
}
CLOSE_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}, "outcome": {"type": "string"}},
                "required": ["summary", "outcome"], "additionalProperties": False}
SENTIMENT_ES = {"positive": "positivo", "neutral": "neutral", "negative": "negativo", "mixed": "mixto"}
REWRITE_MODES = {
    "shorter": "Hazlo más corto y directo, sin perder información.",
    "friendlier": "Hazlo más cálido y cercano, manteniendo el profesionalismo.",
    "formal": "Hazlo más formal y cortés.",
    "fix_grammar": "Corrige ortografía, gramática y puntuación sin cambiar el sentido ni el tono.",
    "translate": "Tradúcelo al idioma indicado conservando el tono.",
}

RULES = (
    "Reglas: escribe como el asesor humano de la empresa (no como bot), en el idioma del cliente, mensajes breves "
    "para WhatsApp (1–3 frases), sin markdown. NUNCA inventes precios, descuentos, disponibilidad, fechas de entrega "
    "ni políticas: solo usa cifras y datos que aparezcan en el contexto; si faltan, propone preguntar o confirmar. "
    "No pidas datos sensibles innecesarios. Las sugerencias deben ser distintas entre sí (por ejemplo: responder, "
    "preguntar para calificar, proponer un siguiente paso).")


class CopilotError(Exception):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def edit_distance(a: str, b: str, cap: int = 2000) -> int:
    """Levenshtein (sobre textos normalizados y recortados)."""
    a, b = _norm(a)[:cap], _norm(b)[:cap]
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


async def settings(session: AsyncSession, org: int) -> dict:
    return await get_setting(session, "copilot", org)


def out(row: CopilotSuggestion) -> dict:
    return {"id": row.id, "kind": row.kind, "content": row.content, "status": row.status,
            "message_id": row.message_id, "agent_id": row.agent_id, "latency_ms": row.latency_ms,
            "created_at": row.created_at}


async def _push(row: CopilotSuggestion, event: str = "copilot.suggestion") -> None:
    if not row.agent_id:
        return
    try:
        from app.realtime import hub

        sender = getattr(hub, "send_to_agent", None)
        if sender:
            await sender(row.organization_id, row.agent_id, event,
                         {"conversation_id": row.conversation_id, **out(row)})
    except Exception:  # noqa: BLE001 — el aviso en vivo nunca debe romper la generación
        log.debug("No se pudo enviar la sugerencia en vivo", exc_info=True)


async def _get(session: AsyncSession, suggestion_id: int) -> CopilotSuggestion | None:
    return (await session.scalars(select(CopilotSuggestion).where(CopilotSuggestion.id == suggestion_id))).first()


# --- Sugerencias de respuesta + siguiente acción -----------------------------------------------------------
async def recent_count(session: AsyncSession, conv_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(CopilotSuggestion).where(
        CopilotSuggestion.conversation_id == conv_id, CopilotSuggestion.kind == "reply",
        CopilotSuggestion.created_at >= utcnow() - timedelta(hours=1))) or 0


async def latest(session: AsyncSession, conv_id: int, kind: str) -> CopilotSuggestion | None:
    return (await session.scalars(select(CopilotSuggestion).where(
        CopilotSuggestion.conversation_id == conv_id, CopilotSuggestion.kind == kind)
        .order_by(CopilotSuggestion.created_at.desc(), CopilotSuggestion.id.desc()).limit(1))).first()


async def generate(session: AsyncSession, conv: Conversation, *, force: bool = False,
                   agent: Agent | None = None) -> dict:
    """Sugerencias para el último mensaje del cliente. Devuelve {reply, next_action, skipped?}.

    Solo en conversaciones atendidas por un asesor. Caché por mensaje del cliente; límite por hora (automático).
    """
    cfg = await settings(session, conv.organization_id)
    if not cfg.get("enabled"):
        return {"skipped": "disabled"}
    if conv.status != "human":
        return {"skipped": "not_human"}
    agent = agent or (await session.get(Agent, conv.assigned_agent_id) if conv.assigned_agent_id else None)
    ctx = await build(session, conv, agent)
    if not ctx.last_customer_message_id:
        return {"skipped": "no_customer_message"}
    cached = (await session.scalars(select(CopilotSuggestion).where(
        CopilotSuggestion.conversation_id == conv.id, CopilotSuggestion.kind == "reply",
        CopilotSuggestion.message_id == ctx.last_customer_message_id)
        .order_by(CopilotSuggestion.created_at.desc()).limit(1))).first()
    if cached and not force:
        nxt = await latest(session, conv.id, "next_action")
        return {"reply": out(cached), "next_action": out(nxt) if nxt else None, "cached": True}
    limit = int(cfg.get("max_per_conversation_per_hour") or 12) * (2 if force else 1)
    if await recent_count(session, conv.id) >= limit:
        return {"skipped": "rate_limited"}

    n = max(1, min(int(cfg.get("max_suggestions") or 3), 5))
    system = (f"Eres el copiloto de los asesores de {ctx.company}. Propones hasta {n} respuestas listas para enviar "
              "y la siguiente mejor acción comercial.\n" + RULES +
              "\nSiguiente mejor acción: elige UNA de " + ", ".join(ACTIONS) +
              ". send_product (payload.sku y payload.text con el mensaje), offer_appointment (payload.text, "
              "payload.date si el cliente la dio), send_template (payload.template), ask_field (payload.field y "
              "payload.text con la pregunta: prioriza datos faltantes del cliente), handoff (payload.group), "
              "create_deal / move_stage (payload.pipeline y payload.stage), none si no hay nada útil. Completa los "
              "campos del payload que no apliquen con null.")
    gaps = f"\nDatos que faltan del cliente: {', '.join(ctx.golden_gaps)}" if ctx.golden_gaps else ""
    data, call_id, latency = await llm.call_json(conv.organization_id, "reply", system, ctx.prompt_block() + gaps,
                                                 REPLY_SCHEMA, conversation_id=conv.id)
    evidence = ctx.evidence_text()
    options, rejected = [], []
    for s in data.get("suggestions") or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        flags = guard.check(text, evidence, ctx.has_catalog)
        (rejected if flags else options).append({"text": text, "flags": flags} if flags else text)
        if len(options) >= n:
            break
    reply = CopilotSuggestion(
        organization_id=conv.organization_id, conversation_id=conv.id, agent_id=agent.id if agent else None,
        message_id=ctx.last_customer_message_id, kind="reply",
        content={"options": options, "text": options[0] if options else None, "rejected": rejected},
        latency_ms=latency, ai_call_id=call_id)
    session.add(reply)
    nxt_row = None
    action = data.get("next_action") or {}
    if (cfg.get("next_action", True) and action.get("action") in ACTIONS and action.get("action") != "none"):
        payload = {k: v for k, v in (action.get("payload") or {}).items() if v not in (None, "")}
        flags = guard.check(payload.get("text") or "", evidence, ctx.has_catalog)
        if flags:
            payload.pop("text", None)
        nxt_row = CopilotSuggestion(
            organization_id=conv.organization_id, conversation_id=conv.id, agent_id=agent.id if agent else None,
            message_id=ctx.last_customer_message_id, kind="next_action",
            content={"action": action["action"], "label": action.get("label") or action["action"], "payload": payload,
                     "reason": action.get("reason") or "", "confidence": action.get("confidence"), "flags": flags},
            latency_ms=latency, ai_call_id=call_id)
        session.add(nxt_row)
    await session.commit()
    await _push(reply)
    if nxt_row is not None:
        await _push(nxt_row, "copilot.next_action")
    return {"reply": out(reply), "next_action": out(nxt_row) if nxt_row else None}


async def record_outcome(session: AsyncSession, row: CopilotSuggestion, status: str, final_text: str | None) -> dict:
    """accepted / edited / dismissed. Con final_text se decide aceptada (sin cambios) o editada y la distancia."""
    if status not in ("accepted", "edited", "dismissed"):
        raise CopilotError("Estado inválido")
    if status != "dismissed" and final_text is not None:
        options = list(row.content.get("options") or []) + [row.content.get("text")]
        options = [o for o in options if isinstance(o, str) and o]
        dist = min((edit_distance(o, final_text) for o in options), default=None)
        row.edit_distance = dist
        row.final_text = final_text[:4000]
        status = "accepted" if dist == 0 else "edited"
    row.status, row.decided_at = status, utcnow()
    await session.commit()
    return out(row) | {"edit_distance": row.edit_distance, "final_text": row.final_text}


# --- Borrador y reescritura ---------------------------------------------------------------------------------
async def draft(session: AsyncSession, conv: Conversation, agent: Agent, instruction: str | None) -> dict:
    ctx = await build(session, conv, agent)
    system = (f"Eres el copiloto de los asesores de {ctx.company}. Redacta UN mensaje listo para enviar al "
              f"cliente siguiendo la instrucción del asesor.\n{RULES}")
    user = ctx.prompt_block() + f"\n\n## Instrucción del asesor\n{(instruction or 'Responde al último mensaje').strip()}"
    data, call_id, latency = await llm.call_json(conv.organization_id, "draft", system, user, TEXT_SCHEMA,
                                                 conversation_id=conv.id)
    text = (data.get("text") or "").strip()
    flags = guard.check(text, ctx.evidence_text() + "\n" + (instruction or ""), ctx.has_catalog)
    row = CopilotSuggestion(organization_id=conv.organization_id, conversation_id=conv.id, agent_id=agent.id,
                            message_id=ctx.last_customer_message_id, kind="draft",
                            content={"text": text, "instruction": instruction, "flags": flags},
                            latency_ms=latency, ai_call_id=call_id)
    session.add(row)
    await session.commit()
    return out(row)


async def rewrite(session: AsyncSession, org: int, agent: Agent, text: str, mode: str, target_lang: str | None,
                  conversation_id: int | None) -> dict:
    if mode not in REWRITE_MODES:
        raise CopilotError(f"Modo inválido: {', '.join(REWRITE_MODES)}")
    if not (text or "").strip():
        raise CopilotError("Escribe el texto a reescribir")
    goal = REWRITE_MODES[mode] + (f" Idioma: {target_lang}." if mode == "translate" and target_lang else "")
    system = ("Reescribes mensajes de asesores para clientes por WhatsApp. " + goal +
              " No agregues información, cifras ni promesas que no estén en el texto original. Sin markdown.")
    data, call_id, latency = await llm.call_json(org, "rewrite", system, text[:4000], TEXT_SCHEMA,
                                                 conversation_id=conversation_id)
    result = (data.get("text") or "").strip()
    flags = guard.check(result, text, has_catalog=True)  # cifras nuevas que no estaban en el original
    row_out = {"text": result if not flags else text, "flags": flags, "mode": mode}
    if conversation_id:
        row = CopilotSuggestion(organization_id=org, conversation_id=conversation_id, agent_id=agent.id,
                                kind="translate" if mode == "translate" else "rewrite",
                                content={"original": text[:4000], **row_out}, latency_ms=latency, ai_call_id=call_id)
        session.add(row)
        await session.commit()
        row_out["id"] = row.id
    return row_out


# --- Resúmenes ------------------------------------------------------------------------------------------------
def format_handoff(d: dict) -> str:
    lines = [f"• Necesidad: {d.get('need') or '—'}"]
    if d.get("captured_data"):
        lines.append("• Datos capturados: " + "; ".join(d["captured_data"]))
    if d.get("bot_promises"):
        lines.append("• Lo que prometió el bot: " + "; ".join(d["bot_promises"]))
    lines.append(f"• Sentimiento: {SENTIMENT_ES.get(d.get('sentiment'), d.get('sentiment') or '—')}")
    lines.append(f"• Siguiente paso: {d.get('next_step') or '—'}")
    return "\n".join(lines)


async def handoff_summary(session: AsyncSession, conv: Conversation) -> str | None:
    cfg = await settings(session, conv.organization_id)
    if not (cfg.get("enabled") and cfg.get("handoff_summary")):
        return None
    msgs = await history(session, conv, 40)
    if not msgs:
        return None
    company = (await get_setting(session, "company", conv.organization_id)).get("name") or "la empresa"
    system = (f"Resume para el asesor de {company} que recibe esta conversación transferida. Sé concreto: qué "
              "necesita el cliente, qué datos ya dio, qué le prometió el bot (citas, llamadas, precios ya "
              "mencionados), cómo se siente y cuál es el siguiente paso recomendado. No inventes nada.")
    data, call_id, latency = await llm.call_json(conv.organization_id, "summary", system, transcript_of(msgs),
                                                 HANDOFF_SCHEMA, conversation_id=conv.id)
    text = format_handoff(data)
    conv.handoff_summary, conv.handoff_summary_at = text, utcnow()
    row = CopilotSuggestion(organization_id=conv.organization_id, conversation_id=conv.id,
                            agent_id=conv.assigned_agent_id, kind="summary",
                            content={"type": "handoff", "text": text, **data}, latency_ms=latency, ai_call_id=call_id)
    session.add(row)
    await session.commit()
    await _push(row, "copilot.summary")
    return text


async def conversation_summary(session: AsyncSession, conv: Conversation, *, on_close: bool = False) -> str | None:
    cfg = await settings(session, conv.organization_id)
    if not cfg.get("enabled") or (on_close and not cfg.get("close_summary")):
        return None
    msgs = await history(session, conv, 80)
    if not msgs:
        return None
    system = ("Resume la conversación en 2–4 frases para el historial del cliente: motivo, lo que se resolvió o "
              "acordó y pendientes. En outcome, una frase con el resultado. No inventes datos.")
    data, call_id, latency = await llm.call_json(conv.organization_id, "summary", system, transcript_of(msgs),
                                                 CLOSE_SCHEMA, conversation_id=conv.id)
    conv.summary = (data.get("summary") or "").strip()
    conv.summary_at = utcnow()
    session.add(CopilotSuggestion(organization_id=conv.organization_id, conversation_id=conv.id,
                                  agent_id=conv.assigned_agent_id, kind="summary",
                                  content={"type": "close" if on_close else "on_demand", "text": conv.summary,
                                           "outcome": data.get("outcome")},
                                  latency_ms=latency, ai_call_id=call_id))
    await session.commit()
    return conv.summary


# --- Siguiente acción: ejecución en el servidor ------------------------------------------------------------
async def execute_action(session: AsyncSession, row: CopilotSuggestion, conv: Conversation, agent: Agent) -> dict:
    """create_deal / move_stage / send_product se ejecutan aquí; las demás las resuelve el panel
    (insertar texto, abrir el modal de plantilla, transferir)."""
    action = row.content.get("action")
    payload = row.content.get("payload") or {}
    result: dict = {"action": action, "client_action": None, "insert_text": payload.get("text")}
    if action in ("create_deal", "move_stage"):
        from app.agent_config import set_stage

        if not (payload.get("pipeline") and payload.get("stage")):
            raise CopilotError("La acción no trae línea y etapa")
        deal = await set_stage(session, conv, payload["pipeline"], payload["stage"], row.content.get("reason"),
                               source="agent", agent_id=agent.id)
        result["deal_id"] = deal.id
    elif action == "send_product":
        from app.interaction_products import add_product

        if payload.get("sku") or payload.get("product_name"):
            item, _created = await add_product(session, conv, stage="quoted", source="agent",
                                               external_ref=payload.get("sku"), name=payload.get("product_name"),
                                               created_by=agent.id)
            result["interaction_product_id"] = item.id
        await session.commit()
    else:
        result["client_action"] = {"send_template": "open_template", "handoff": "open_transfer"}.get(action, "insert_text")
    row.status, row.decided_at = "accepted", utcnow()
    await session.commit()
    return result


async def overview(session: AsyncSession, conv: Conversation) -> dict:
    reply = await latest(session, conv.id, "reply")
    nxt = await latest(session, conv.id, "next_action")
    fresh =bool(reply and conv.last_inbound_at and reply.created_at >= conv.last_inbound_at)
    return {"reply": out(reply) if reply else None, "next_action": out(nxt) if nxt else None,
            "fresh": fresh, "handoff_summary": conv.handoff_summary, "handoff_summary_at": conv.handoff_summary_at,
            "summary": conv.summary, "summary_at": conv.summary_at}


__all__ = ["CopilotContext", "CopilotError", "draft", "edit_distance", "execute_action", "generate",
           "handoff_summary", "conversation_summary", "overview", "record_outcome", "rewrite"]
