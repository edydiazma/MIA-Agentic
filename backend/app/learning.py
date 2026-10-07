"""Aprendizaje a partir de conversaciones reales (vía Cortex, propósito "learning"):

1. Generar memoria: la IA lee conversaciones cerradas (sobre todo ventas) y propone conocimiento reutilizable
   (FAQ, objeciones, respuestas ganadoras, datos, políticas, hallazgos). Un administrador lo aprueba antes de
   que los agentes lo usen (memory_items.status = approved).
2. El mejor vendedor: ranking de asesores por ventas (tipificaciones is_success); de las conversaciones ganadas
   de los mejores se genera un playbook y un prompt de agente vendedor, que se revisa antes de crear el agente.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import CallContext, complete_json, resolve_cortex
from app.ai.structured import LLMError
from app.classifier import render_message
from app.db import SessionLocal
from app.models import (
    Agent,
    AIAgent,
    Conversation,
    LearningRun,
    MemoryItem,
    Message,
    SellerProfile,
    Typification,
    utcnow,
)
from app.settings_store import add_revision, get_setting

log = logging.getLogger(__name__)

MEMORY_KINDS = {
    "faq": "Pregunta frecuente y su respuesta correcta",
    "objection": "Objeción típica del cliente y cómo responderla",
    "winning_response": "Respuesta o técnica de un asesor que ayudó a cerrar una venta",
    "fact": "Dato del negocio (precio, condición, horario, sede, requisito)",
    "policy": "Regla o política que se aplicó (garantías, devoluciones, descuentos)",
    "insight": "Patrón del comportamiento de los clientes útil para vender o atender",
}
_tasks: set[asyncio.Task] = set()


async def cortex_id_for(session: AsyncSession, org: int, override: int | None = None) -> int | None:
    """Cortex del aprendizaje: el indicado, el de learning, el del clasificador o el principal (None)."""
    if override:
        return override
    learning = await get_setting(session, "learning", org)
    if learning.get("cortex_id"):
        return learning["cortex_id"]
    return (await get_setting(session, "classifier", org)).get("cortex_id")


def _bounds(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    # Sin fecha final: hasta ahora (UTC). date.today() es la fecha local del servidor y dejaría fuera
    # lo cerrado hoy en UTC.
    end_dt = (datetime.combine(end, datetime.min.time(), UTC) + timedelta(days=1)) if end else utcnow() + timedelta(
        seconds=1)
    start_dt = datetime.combine(start, datetime.min.time(), UTC) if start else end_dt - timedelta(days=90)
    return start_dt, end_dt


async def transcript(session: AsyncSession, conv: Conversation, names: dict[int, str], limit: int = 80) -> str:
    rows = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).all()
    lines = [t for t in (render_message(m, names) for m in reversed(rows)) if t]
    result = conv.typification.name if conv.typification else "sin tipificar"
    return f"### Conversación {conv.id} (resultado: {result})\n" + "\n".join(lines)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _finish(session: AsyncSession, run: LearningRun, stats: dict, error: str | None = None) -> None:
    run.stats, run.error = stats, error
    run.status = "failed" if error else "done"
    run.finished_at = utcnow()
    await session.commit()


# --- 1. Generar memoria ---------------------------------------------------------
MEMORY_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "conversation_ids": {"type": "array", "items": {"type": "integer"}},
            "confidence": {"type": "number"},
        },
        "required": ["kind", "title", "content", "conversation_ids", "confidence"],
        "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}


def memory_system(existing: list[MemoryItem]) -> str:
    kinds = "\n".join(f"- {k}: {v}" for k, v in MEMORY_KINDS.items())
    known = "\n".join(f"- [{m.kind}] {m.title}" for m in existing[:300]) or "(vacía)"
    return (
        "Eres analista de un equipo comercial que atiende por WhatsApp. Lee las conversaciones y extrae "
        "conocimiento reutilizable para que agentes de IA y asesores atiendan y vendan mejor.\n\n"
        f"Tipos de memoria:\n{kinds}\n\n"
        "Reglas:\n- Solo conocimiento que se repite o que se confirmó en la conversación; nada inventado.\n"
        "- Escribe el contenido listo para usar (la respuesta exacta, el dato exacto), en 1 a 5 frases.\n"
        "- Sin datos personales de clientes (nombres, teléfonos, documentos).\n"
        "- No repitas lo que ya está en la memoria actual; si algo la contradice o la mejora, propónlo "
        "con un título que empiece por «Actualización:».\n"
        "- Indica en conversation_ids de dónde sale cada punto y una confianza de 0 a 1.\n\n"
        f"Memoria actual:\n{known}"
    )


async def start_memory_run(session: AsyncSession, agent: Agent, params: dict) -> LearningRun:
    run = LearningRun(organization_id=agent.organization_id, type="memory", params=params, created_by=agent.id,
                      cortex_id=params.get("cortex_id"))
    session.add(run)
    await session.commit()
    _spawn(run_memory(run.id))
    return run


async def run_memory(run_id: int) -> None:
    async with SessionLocal() as session:
        run = await session.get(LearningRun, run_id)
        org = run.organization_id
        cfg = await get_setting(session, "learning", org)
        p = run.params or {}
        stats = {"conversations": 0, "batches": 0, "proposed": 0, "duplicates": 0, "failed_batches": 0}
        try:
            start, end = _bounds(date.fromisoformat(p["start"]) if p.get("start") else None,
                                 date.fromisoformat(p["end"]) if p.get("end") else None)
            stmt = select(Conversation).where(Conversation.organization_id == org, Conversation.status == "closed",
                                              Conversation.closed_at >= start, Conversation.closed_at < end)
            if p.get("typifications"):
                stmt = stmt.join(Typification, Typification.id == Conversation.typification_id).where(
                    Typification.name.in_(p["typifications"]))
            limit = int(p.get("max_conversations") or cfg["max_conversations"])
            convs = (await session.scalars(stmt.order_by(Conversation.closed_at.desc()).limit(limit))).unique().all()
            stats["conversations"] = len(convs)
            if not convs:
                return await _finish(session, run, stats, "No hay conversaciones cerradas con esos filtros")

            cx = await resolve_cortex(session, org, await cortex_id_for(session, org, run.cortex_id), "learning")
            names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
            existing = list((await session.scalars(select(MemoryItem).where(
                MemoryItem.organization_id == org, MemoryItem.status != "rejected"))).all())
            seen = {m.title.strip().lower() for m in existing}
            size = max(1, int(cfg["batch_size"]))
            for i in range(0, len(convs), size):
                batch = convs[i:i + size]
                text = "\n\n".join([await transcript(session, c, names) for c in batch])
                stats["batches"] += 1
                ctx = CallContext(organization_id=org, purpose="learning")
                try:
                    out = await complete_json(session, cx, memory_system(existing), text, MEMORY_SCHEMA, ctx,
                                              max_tokens=8000)
                except LLMError as e:
                    log.warning("Lote de memoria fallido: %s", e)
                    stats["failed_batches"] += 1
                    continue
                valid_ids = {c.id for c in batch}
                for item in out.get("items", []):
                    title = str(item.get("title") or "").strip()[:255]
                    content = str(item.get("content") or "").strip()
                    if not title or not content or item.get("kind") not in MEMORY_KINDS:
                        continue
                    if title.lower() in seen:
                        stats["duplicates"] += 1
                        continue
                    seen.add(title.lower())
                    try:
                        confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    m = MemoryItem(organization_id=org, kind=item["kind"], title=title, content=content,
                                   status="pending", source="learned", confidence=confidence, run_id=run.id,
                                   evidence_conversation_ids=[int(i) for i in item.get("conversation_ids") or []
                                                              if isinstance(i, int) and i in valid_ids])
                    session.add(m)
                    existing.append(m)
                    stats["proposed"] += 1
                await session.commit()
            error = "Todas las llamadas al modelo fallaron" if stats["failed_batches"] == stats["batches"] else None
            await _finish(session, run, stats, error)
        except Exception as e:
            log.exception("Falló la generación de memoria")
            await session.rollback()
            await _finish(session, run, stats, f"{type(e).__name__}: {e}"[:1000])


async def approved_memory(session: AsyncSession, org: int, limit: int = 400) -> list[MemoryItem]:
    """Memoria aprobada (la usan los agentes con use_memory)."""
    return list((await session.scalars(
        select(MemoryItem).where(MemoryItem.organization_id == org, MemoryItem.status == "approved")
        .order_by(MemoryItem.kind, MemoryItem.id).limit(limit))).all())


# --- 2. El mejor vendedor -------------------------------------------------------
async def rank_agents(session: AsyncSession, org: int, start: date | None, end: date | None) -> list[dict]:
    lo, hi = _bounds(start, end)
    rows = (await session.execute(
        select(Conversation.assigned_agent_id, func.count(),
               func.count().filter(Typification.is_success))
        .outerjoin(Typification, Typification.id == Conversation.typification_id)
        .where(Conversation.organization_id == org, Conversation.closed_at >= lo, Conversation.closed_at < hi,
               Conversation.assigned_agent_id.is_not(None))
        .group_by(Conversation.assigned_agent_id))).all()
    agents = {a.id: a for a in (await session.scalars(select(Agent).where(Agent.organization_id == org))).all()}
    out = [{"agent_id": aid, "name": agents[aid].name if aid in agents else f"#{aid}", "closed": closed,
            "sales": sales, "conversion_pct": round(100 * sales / closed, 1) if closed else None}
           for aid, closed, sales in rows]
    # Más ventas primero; a igualdad, mejor conversión
    return sorted(out, key=lambda r: (-r["sales"], -(r["conversion_pct"] or 0)))


PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "persona": {"type": "string", "description": "Quién es el vendedor y cómo se presenta"},
        "tone": {"type": "string"},
        "sales_process": {"type": "array", "items": {
            "type": "object",
            "properties": {"stage": {"type": "string"}, "goal": {"type": "string"},
                           "tactics": {"type": "array", "items": {"type": "string"}}},
            "required": ["stage", "goal", "tactics"], "additionalProperties": False}},
        "discovery_questions": {"type": "array", "items": {"type": "string"}},
        "objections": {"type": "array", "items": {
            "type": "object",
            "properties": {"objection": {"type": "string"}, "response": {"type": "string"}},
            "required": ["objection", "response"], "additionalProperties": False}},
        "closing_techniques": {"type": "array", "items": {"type": "string"}},
        "do": {"type": "array", "items": {"type": "string"}},
        "dont": {"type": "array", "items": {"type": "string"}},
        "example_phrases": {"type": "array", "items": {"type": "string"}},
        "system_prompt": {"type": "string",
                          "description": "Prompt completo, en segunda persona, para un agente de IA vendedor por WhatsApp"},
    },
    "required": ["persona", "tone", "sales_process", "discovery_questions", "objections", "closing_techniques",
                 "do", "dont", "example_phrases", "system_prompt"],
    "additionalProperties": False,
}

SELLER_SYSTEM = (
    "Eres un director comercial experto. Estudia conversaciones de WhatsApp que terminaron en venta, atendidas por "
    "los mejores asesores de la empresa, y destila cómo venden: proceso, preguntas para descubrir necesidades, "
    "manejo de objeciones, técnicas de cierre, tono y frases que funcionan. Básate solo en lo que se ve en las "
    "conversaciones; no inventes precios, promociones ni políticas. Sin datos personales de clientes.\n\n"
    "Luego escribe `system_prompt`: el prompt de un agente de IA que vende por WhatsApp como estos asesores. "
    "Debe incluir su rol, tono, proceso de venta paso a paso, preguntas de descubrimiento, respuestas a "
    "objeciones y cierre; mensajes breves; usar el catálogo y la memoria del negocio para datos concretos; "
    "y transferir a un asesor humano cuando el cliente esté listo para pagar, pida algo que no puede resolver "
    "o lo solicite."
)


async def start_seller_run(session: AsyncSession, agent: Agent, params: dict) -> LearningRun:
    run = LearningRun(organization_id=agent.organization_id, type="seller", params=params, created_by=agent.id,
                      cortex_id=params.get("cortex_id"))
    session.add(run)
    await session.commit()
    _spawn(run_seller(run.id))
    return run


async def run_seller(run_id: int) -> None:
    async with SessionLocal() as session:
        run = await session.get(LearningRun, run_id)
        org = run.organization_id
        cfg = await get_setting(session, "learning", org)
        p = run.params or {}
        stats: dict = {"conversations": 0}
        try:
            agent_ids = [int(a) for a in p.get("agent_ids") or []]
            if not agent_ids:
                ranking = await rank_agents(session, org, None, None)
                agent_ids = [r["agent_id"] for r in ranking[:2] if r["sales"]]
            if not agent_ids:
                return await _finish(session, run, stats, "No hay asesores con ventas registradas")
            limit = int(p.get("max_conversations") or cfg["max_conversations"])
            convs = (await session.scalars(
                select(Conversation).join(Typification, Typification.id == Conversation.typification_id)
                .where(Conversation.organization_id == org, Conversation.assigned_agent_id.in_(agent_ids),
                       Typification.is_success)
                .order_by(Conversation.closed_at.desc()).limit(limit))).unique().all()
            stats["conversations"] = len(convs)
            if not convs:
                return await _finish(session, run, stats, "Esos asesores no tienen conversaciones ganadas")

            names = dict((await session.execute(select(Agent.id, Agent.name).where(Agent.organization_id == org))).all())
            text = "\n\n".join([await transcript(session, c, names, limit=60) for c in convs])
            approved = [m for m in await approved_memory(session, org) if m.kind in ("objection", "winning_response")]
            if approved:
                text += "\n\n## Memoria aprobada relevante\n" + "\n".join(f"- {m.title}: {m.content}" for m in approved)
            cx = await resolve_cortex(session, org, await cortex_id_for(session, org, run.cortex_id), "learning")
            playbook = await complete_json(session, cx, SELLER_SYSTEM, text, PLAYBOOK_SCHEMA,
                                           CallContext(organization_id=org, purpose="learning"), max_tokens=12000)
            stats["agents"] = [names.get(a, f"#{a}") for a in agent_ids]
            profile = SellerProfile(
                organization_id=org, name=p.get("name") or "Mejor vendedor", source_agent_ids=agent_ids, stats=stats,
                playbook={k: v for k, v in playbook.items() if k != "system_prompt"},
                system_prompt=playbook["system_prompt"], run_id=run.id)
            session.add(profile)
            await session.flush()
            stats["profile_id"] = profile.id
            await _finish(session, run, dict(stats))
        except Exception as e:
            log.exception("Falló la creación del mejor vendedor")
            await session.rollback()
            await _finish(session, run, stats, f"{type(e).__name__}: {e}"[:1000])


async def create_agent_from_profile(session: AsyncSession, profile: SellerProfile, base: AIAgent | None,
                                    created_by: int | None) -> AIAgent:
    """Crea un agente de IA a partir del playbook (desactivado hasta que un administrador lo revise)."""
    name = profile.name
    if await session.scalar(select(AIAgent.id).where(AIAgent.organization_id == profile.organization_id,
                                                     AIAgent.name == name)):
        name = f"{name} ({profile.id})"
    agent = AIAgent(
        organization_id=profile.organization_id, name=name, enabled=False,
        description="Agente vendedor aprendido de los mejores asesores",
        cortex_id=base.cortex_id if base else None, system_prompt=profile.system_prompt,
        handoff_message=base.handoff_message if base else "Te comunico con un asesor, en un momento te atiende.",
    )
    session.add(agent)
    await session.flush()
    profile.ai_agent_id, profile.status = agent.id, "applied"
    await add_revision(session, profile.organization_id, "ai_agent", agent.id,
                       {"name": agent.name, "system_prompt": agent.system_prompt, "seller_profile_id": profile.id},
                       "ai", created_by)
    await session.commit()
    return agent
