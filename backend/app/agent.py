"""Agente de IA: arma el contexto multimodal, llama al Cortex (con failover) y responde por WhatsApp."""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import appointments, bot_outbox, storage
from app.ai import router
from app.ai.base import AgentRequest, DocumentPart, ImagePart, Part, TextPart, ToolSpec, Turn
from app.config import get_settings
from app.db import SessionLocal, set_actor
from app.identity import wa_address
from app.models import (
    AIAgent,
    AIAgentKnowledge,
    Appointment,
    Conversation,
    Group,
    KnowledgeDoc,
    MemoryItem,
    Message,
    Product,
    utcnow,
)
from app.realtime import hub
from app.service import handoff, record_message, wa_client
from app.settings_store import get_setting

settings = get_settings()
log = logging.getLogger(__name__)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DOC_MIMES = {"application/pdf"}
TEXT_DOC_MIMES = {"text/plain", "text/csv", "text/markdown", "application/json"}
KB_WARN_CHARS = 600_000
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MEMORY_TITLES = {
    "faq": "Preguntas frecuentes", "objection": "Objeciones y cómo responderlas",
    "winning_response": "Respuestas que funcionan", "fact": "Datos del negocio", "policy": "Políticas",
    "insight": "Hallazgos sobre los clientes",
}

CHANNEL_RULES = (
    "\n\n## Canal\nEstás respondiendo por WhatsApp. Escribe mensajes breves y naturales, sin encabezados "
    "markdown ni tablas; usa *negritas* de WhatsApp con moderación. Puedes recibir texto, imágenes, notas "
    "de voz (te llegan transcritas), PDFs y ubicaciones: úsalos para entender al cliente. Responde solo con "
    "información de estas instrucciones, la base de conocimiento, la memoria del negocio y el catálogo; si no "
    "la tienes, dilo y ofrece transferir a un asesor. Nunca inventes precios, disponibilidad ni políticas."
)


def handoff_tool(group_names: list[str]) -> ToolSpec:
    group_hint = (f" Grupos disponibles: {', '.join(group_names)}. Elige el más adecuado o deja vacío si no aplica."
                  if group_names else " Deja el grupo vacío.")
    return ToolSpec(
        name="transfer_to_human",
        description=(
            "Transfiere la conversación a un asesor humano. Úsala cuando el cliente lo pida explícitamente, "
            "cuando esté molesto, cuando la solicitud requiera una acción que no puedes realizar (pagos, "
            "reclamos formales, excepciones a políticas) o cuando no tengas la información para resolverla. "
            "Después de usarla, despídete brevemente indicando que un asesor continuará." + group_hint
        ),
        schema={"type": "object", "properties": {
            "reason": {"type": "string", "description": "Motivo breve para el asesor"},
            "group": {"type": "string", "description": "Nombre del grupo de destino, o vacío"}},
            "required": ["reason", "group"], "additionalProperties": False},
    )


CHECK_AVAILABILITY = ToolSpec(
    name="check_availability",
    description="Consulta los horarios libres para agendar una cita en una fecha (formato AAAA-MM-DD).",
    schema={"type": "object", "properties": {"date": {"type": "string", "description": "Fecha AAAA-MM-DD"}},
            "required": ["date"], "additionalProperties": False},
)
BOOK_APPOINTMENT = ToolSpec(
    name="book_appointment",
    description=("Agenda una cita para el cliente. Úsala solo después de consultar disponibilidad y de que el "
                 "cliente confirme fecha y hora explícitamente."),
    schema={"type": "object", "properties": {
        "date": {"type": "string", "description": "Fecha AAAA-MM-DD"},
        "time": {"type": "string", "description": "Hora HH:MM (24 h), uno de los horarios libres"},
        "notes": {"type": "string", "description": "Detalles: motivo, producto de interés, etc."}},
        "required": ["date", "time", "notes"], "additionalProperties": False},
)
SEARCH_PRODUCTS = ToolSpec(
    name="search_products",
    description=("Busca productos en el catálogo (nombre, categoría, marca, atributos). Úsala antes de dar precios, "
                 "disponibilidad o características. Devuelve SKU, precio, stock y enlace."),
    schema={"type": "object", "properties": {
        "query": {"type": "string", "description": "Lo que busca el cliente"},
        "max_price": {"type": "number", "description": "Precio máximo, o 0 si no aplica"},
        "category": {"type": "string", "description": "Categoría, o vacío"}},
        "required": ["query", "max_price", "category"], "additionalProperties": False},
)
SEND_PRODUCT = ToolSpec(
    name="send_product",
    description="Envía al cliente la ficha de un producto del catálogo (foto, precio y enlace) usando su SKU.",
    schema={"type": "object", "properties": {"sku": {"type": "string", "description": "SKU devuelto por search_products"}},
            "required": ["sku"], "additionalProperties": False},
)

# --- Debounce: los clientes suelen mandar varios mensajes seguidos -------------
_pending: dict[int, asyncio.Task] = {}
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def schedule_reply(conversation_id: int) -> None:
    task = _pending.get(conversation_id)
    if task and not task.done():
        task.cancel()  # solo puede estar en la espera; aún no llamó al modelo
    _pending[conversation_id] = asyncio.create_task(_delayed_reply(conversation_id))


async def _delayed_reply(conversation_id: int) -> None:
    try:
        await asyncio.sleep(settings.debounce_seconds)
    except asyncio.CancelledError:
        return
    _pending.pop(conversation_id, None)  # desde aquí ya no se cancela
    async with _locks[conversation_id]:
        try:
            await run_agent(conversation_id)
        except Exception:
            log.exception("El agente falló en la conversación %s", conversation_id)


# --- Contexto -------------------------------------------------------------------
def _media_ids(history: list[Message]) -> set[int]:
    """Solo las últimas imágenes/documentos del cliente van completos al modelo."""
    budget, ids = settings.max_images_in_context, set()
    for m in reversed(history):
        if m.direction == "in" and m.media_path and budget > 0:
            ids.add(m.id)
            budget -= 1
    return ids


async def prefetch_media(history: list[Message]) -> dict[int, bytes]:
    wanted, out = _media_ids(history), {}
    for m in history:
        if m.id in wanted:
            try:
                out[m.id] = await storage.download(m.media_path)
            except Exception:
                log.warning("No se pudo leer el medio del mensaje %s", m.id)
    return out


def _parts_for(m: Message, media: bytes | None) -> list[Part]:
    parts: list[Part] = []
    label = {"agent": "[Asesor humano] ", "campaign": "[Campaña] "}.get(m.sender_type, "")
    if m.type == "audio":
        parts.append(TextPart(f"[Nota de voz] {m.transcript}" if m.transcript else "[Nota de voz sin transcribir]"))
    elif m.type in ("image", "sticker") and m.media_path:
        if media is not None and m.media_mime in IMAGE_MIMES:
            parts.append(ImagePart(media, m.media_mime))
        else:
            parts.append(TextPart("[Imagen enviada anteriormente]"))
    elif m.type == "document" and m.media_path:
        name = m.media_filename or "documento"
        if media is not None and m.media_mime in DOC_MIMES:
            parts.append(DocumentPart(media, m.media_mime, name))
        elif media is not None and m.media_mime in TEXT_DOC_MIMES:
            parts.append(TextPart(f"[Documento {name}]\n{media.decode('utf-8', 'replace')[:50_000]}"))
        else:
            parts.append(TextPart(f"[Documento {name} ({m.media_mime}) — no se puede leer este formato]"))
    elif m.type == "video":
        parts.append(TextPart("[Video recibido]"))
    if m.text:
        parts.append(TextPart(label + m.text))
    if not parts:
        parts.append(TextPart(f"[{m.type}]"))
    return parts


def build_turns(history: list[Message], media: dict[int, bytes] | None = None) -> list[Turn]:
    """Convierte el historial en turnos alternados (media = bytes ya descargados por prefetch_media)."""
    media = media or {}
    turns: list[Turn] = []
    for m in history:
        if m.sender_type == "system":
            continue
        role = "user" if m.direction == "in" else "assistant"
        parts = _parts_for(m, media.get(m.id))
        if role == "assistant":  # el asistente solo devuelve texto
            parts = [p for p in parts if isinstance(p, TextPart)]
        if turns and turns[-1].role == role:
            turns[-1].parts.extend(parts)
        else:
            turns.append(Turn(role=role, parts=parts))
    while turns and turns[0].role != "user":
        turns.pop(0)
    return turns


async def build_system(session: AsyncSession, agent: AIAgent) -> str:
    """Parte estable del prompt (se cachea): instrucciones + reglas + conocimiento + memoria del negocio."""
    system = agent.system_prompt + CHANNEL_RULES
    if agent.max_words:
        system += f"\nResponde con un máximo de {agent.max_words} palabras por mensaje."
    if agent.security_enabled:
        from app.agent_config import SECURITY_DEFAULT

        system += "\n\n## Seguridad\n" + (agent.security_prompt or SECURITY_DEFAULT)
    if agent.use_knowledge:
        docs = (await session.scalars(
            select(KnowledgeDoc).join(AIAgentKnowledge, AIAgentKnowledge.doc_id == KnowledgeDoc.id)
            .where(AIAgentKnowledge.ai_agent_id == agent.id, KnowledgeDoc.enabled).order_by(KnowledgeDoc.id))).all()
        if docs:
            kb = "\n\n".join(f"### {d.title}\n{d.content}" for d in docs)
            if len(kb) > KB_WARN_CHARS:
                log.warning("La base de conocimiento del agente %s es muy grande (%s caracteres)", agent.id, len(kb))
            system += "\n\n## Base de conocimiento\n" + kb
    if agent.use_memory:
        items = (await session.scalars(
            select(MemoryItem).where(MemoryItem.organization_id == agent.organization_id,
                                     MemoryItem.status == "approved").order_by(MemoryItem.kind, MemoryItem.id))).all()
        if items:
            system += "\n\n## Memoria del negocio (aprendida de conversaciones reales)"
            for kind, title in MEMORY_TITLES.items():
                group = [m for m in items if m.kind == kind]
                if group:
                    system += f"\n### {title}\n" + "\n".join(f"- {m.title}: {m.content}" for m in group)
    return system


# --- Ejecución ------------------------------------------------------------------
async def run_agent(conversation_id: int) -> None:
    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation_id)
        if not conv or conv.status != "bot":
            return  # un asesor la tomó mientras esperábamos
        org = conv.organization_id
        agent_id = conv.ai_agent_id or conv.channel.default_ai_agent_id
        agent = await session.get(AIAgent, agent_id) if agent_id else None
        if not agent or not agent.enabled:
            return

        rows = await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                     .order_by(Message.created_at.desc(), Message.id.desc())
                                     .limit(settings.history_limit))
        history = list(reversed(rows.all()))
        if not history or history[-1].direction != "in":
            return  # ya respondimos lo último
        turns = build_turns(history, await prefetch_media(history))
        if not turns:
            return

        groups = {g.name.lower(): g.id for g in (await session.scalars(
            select(Group).where(Group.organization_id == org))).all()}
        appt_cfg, tz = await appointments.config(session, org)
        tools = [handoff_tool(list(groups))]
        if appt_cfg["enabled"] and agent.use_appointments:
            tools += [CHECK_AVAILABILITY, BOOK_APPOINTMENT]
        if agent.use_catalog and await session.scalar(
                select(exists().where(Product.organization_id == org, Product.available))):
            tools += [SEARCH_PRODUCTS, SEND_PRODUCT]
        from app import agent_config  # configuración avanzada: etapas, seguridad, origen del cliente

        stages = await agent_config.ai_stages(session, org)
        if stages:
            tools.append(agent_config.stage_tool(stages))
        if agent.security_enabled:
            tools.append(agent_config.SECURITY_TOOL)
        security_hit: dict[str, str] = {}

        handoff_req: dict[str, object] = {}

        async def execute_tool(name: str, args: dict) -> str:
            if name == "transfer_to_human":
                handoff_req["reason"] = args.get("reason", "")
                handoff_req["group_id"] = groups.get((args.get("group") or "").strip().lower())
                return "Transferencia registrada. Un asesor humano continuará la conversación."
            if name == "check_availability":
                slots = await appointments.available_slots(session, org, date.fromisoformat(args["date"]))
                if not slots:
                    return "No hay horarios libres ese día. Sugiere otra fecha."
                return "Horarios libres: " + ", ".join(s.strftime("%H:%M") for s in slots)
            if name == "book_appointment":
                starts = appointments.parse_local(args["date"], args["time"], tz)
                if starts not in await appointments.available_slots(session, org, starts.date()):
                    return "Ese horario ya no está disponible. Consulta disponibilidad de nuevo."
                appt = Appointment(organization_id=org, contact_id=conv.contact_id, conversation_id=conv.id,
                                   starts_at=starts.astimezone(UTC), duration_min=appt_cfg["duration_min"],
                                   title=appt_cfg["title"], notes=args.get("notes") or None, created_by_type="bot")
                session.add(appt)
                await session.commit()
                await hub.broadcast("appointment.created", {
                    "id": appt.id, "contact_id": conv.contact_id, "starts_at": appt.starts_at.isoformat()},
                    conv.organization_id)
                return f"Cita agendada para el {args['date']} a las {args['time']}. Confírmasela al cliente."
            if name == "search_products":
                from app import catalog  # import diferido (lo implementa el módulo de catálogo)

                found = await catalog.search(session, org, args.get("query") or "",
                                             max_price=args.get("max_price") or None,
                                             category=(args.get("category") or None), limit=5)
                return "\n".join(catalog.describe(p) for p in found) or "No hay productos que coincidan."
            if name == "send_product":
                return await _send_product(session, conv, agent, args.get("sku") or "")
            if name == "set_stage":
                pipeline, _, key = str(args.get("stage") or "").partition(":")
                try:
                    deal = await agent_config.set_stage(session, conv, pipeline, key, args.get("reason"))
                except ValueError as e:
                    return str(e)
                return f"Etapa registrada ({pipeline}:{key}, negocio {deal.id}). No lo menciones al cliente."
            if name == "flag_security":
                security_hit["kind"] = str(args.get("kind") or "abuse")
                security_hit["reason"] = str(args.get("reason") or "")
                return "Registrado. No respondas más a este mensaje."
            raise ValueError(f"Herramienta desconocida: {name}")

        if agent.timezone:
            try:
                tz = ZoneInfo(agent.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("Zona horaria inválida en el agente %s: %s", agent.id, agent.timezone)
        now_local = utcnow().astimezone(tz)
        context = (f"## Contexto\nFecha y hora actual: {WEEKDAYS[now_local.weekday()]} "
                   f"{now_local:%Y-%m-%d %H:%M} ({tz.key}).\n"
                   f"Cliente: {conv.contact.name or 'desconocido'} ({_channel_label(conv)}).")
        if agent.use_customer_memory and conv.contact.memory:
            context += f"\nMemoria del cliente (lo que ya sabemos de él):\n{conv.contact.memory}"
        origin = await agent_config.origin_context(session, conv, agent)
        if origin:
            context += "\n" + origin
        elif conv.ad_headline and agent.ad_context_enabled:
            context += f"\nEl cliente llegó desde el anuncio: «{conv.ad_headline}»."
        context += agent_config.stages_context(stages)
        req = AgentRequest(model="", system=await build_system(session, agent), context=context, turns=turns,
                           tools=tools)

        result = None
        try:
            cx = await router.resolve_cortex(session, org, agent.cortex_id, "chat")
            ctx = router.CallContext(organization_id=org, purpose="chat", conversation_id=conv.id,
                                     ai_agent_id=agent.id)
            result = await router.run_chat(session, cx, req, execute_tool, ctx)
        except router.CortexUnavailable as e:
            log.error("Sin respuesta del Cortex en la conversación %s: %s", conv.id, e)
            handoff_req["reason"] = "La IA no respondió (todas las conexiones fallaron)"
        except Exception as e:
            log.exception("Error del agente de IA")
            handoff_req["reason"] = f"Error del bot: {type(e).__name__}"

        if security_hit:  # spam / abuso: la acción configurada reemplaza la respuesta del modelo
            await agent_config.apply_security(session, conv, agent, security_hit["kind"], security_hit["reason"])
            return
        # Si un asesor tomó la conversación mientras el modelo pensaba, no respondemos.
        await session.refresh(conv)
        if conv.status != "bot":
            return
        # Optimización de costos: los textos del bot seguidos (respuesta + aviso de transferencia, o dos respuestas a
        # ráfagas del cliente) salen como un solo mensaje facturable (app/bot_outbox.py)
        merge = bool(getattr(agent, "cost_optimization", False))
        if result and result.text:
            await set_actor(session, "bot")
            await bot_outbox.queue_text(session, conv, result.text, ai_agent_id=agent.id, merge=merge)
        if handoff_req:
            if not (result and result.text):
                await bot_outbox.queue_text(session, conv, agent.handoff_message, ai_agent_id=agent.id, merge=merge)
            await bot_outbox.flush(conv.id)  # el texto va antes de la transferencia
            await handoff(session, conv, str(handoff_req["reason"]), handoff_req.get("group_id"), actor="bot")


def _channel_label(conv: Conversation) -> str:
    """Canal por el que escribe el cliente (el modelo adapta formato y longitud)."""
    provider = conv.channel.provider
    if provider == "whatsapp_cloud":
        c = conv.contact
        return f"WhatsApp +{c.wa_id}" if c.wa_id else f"WhatsApp @{c.wa_username or 'usuario'}"
    return {"messenger": "Facebook Messenger", "instagram": "Instagram Direct (respuestas cortas, máx. 1000 caracteres)",
            "webchat": "chat del sitio web"}.get(provider, provider)


async def _send_product(session: AsyncSession, conv: Conversation, agent: AIAgent, sku: str) -> str:
    from app import catalog

    p = await session.scalar(select(Product).where(Product.organization_id == conv.organization_id,
                                                   Product.sku == sku))
    if not p:
        return "No existe un producto con ese SKU. Usa search_products."
    cfg = await get_setting(session, "catalog", conv.organization_id)
    client = await wa_client(session, conv.channel, conv)
    caption = catalog.describe(p)[:1000]
    msg = Message(direction="out", sender_type="bot", ai_agent_id=agent.id, text=caption)
    try:
        if (cfg["send_as_catalog_message"] and p.in_meta_catalog and cfg["meta_catalog_id"]
                and conv.channel.provider == "whatsapp_cloud"):
            msg.type = "product"
            msg.wa_message_id = await client.send_product(wa_address(conv.contact), cfg["meta_catalog_id"], p.sku, p.name)
        elif p.image_url:
            msg.type = "image"
            msg.wa_message_id = await client.send_image_link(wa_address(conv.contact), p.image_url, caption)
        else:
            msg.type = "text"
            msg.wa_message_id = (await client.send_text(wa_address(conv.contact), caption))[0]
        msg.status = "sent"
    except Exception as e:
        msg.status, msg.error = "failed", str(e)[:2000]
    msg.metadata_ = {"sku": p.sku, "product_id": p.id}
    await record_message(session, conv, msg)
    if msg.status == "sent":  # producto cotizado al cliente (Cliente 360: productos por interacción)
        from app.interaction_products import add_product

        await add_product(session, conv, stage="quoted", source="catalog_message", product_id=p.id, message_id=msg.id)
        await session.commit()
    return "Producto enviado al cliente." if msg.status == "sent" else f"No se pudo enviar: {msg.error}"
