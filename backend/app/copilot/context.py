"""Contexto de la conversación para el copiloto.

Solo datos no sensibles del cliente (nunca documento, fecha de nacimiento ni dirección del registro maestro).
Se arma una vez por evento y se reutiliza para sugerencias y siguiente acción en la misma llamada.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Agent,
    AIAgent,
    AIAgentKnowledge,
    ContactGolden,
    Conversation,
    Deal,
    InteractionProduct,
    KnowledgeDoc,
    MemoryItem,
    Message,
    QuickReply,
)
from app.settings_store import get_setting

log = logging.getLogger(__name__)

SENDER = {"contact": "Cliente", "bot": "Bot", "agent": "Asesor", "campaign": "Campaña", "flow": "Flujo"}
KB_CHARS = 6000
GOLDEN_SAFE = ("first_name", "last_name", "primary_email", "plates", "vins")  # sin documento, nacimiento ni dirección


@dataclass
class CopilotContext:
    conversation: Conversation
    company: str
    agent_name: str | None
    tone: str
    transcript: str
    last_customer_text: str
    last_customer_message_id: int | None
    customer: str
    business: str
    catalog: str
    has_catalog: bool
    quick_replies: str
    stage: str
    golden_gaps: list[str] = field(default_factory=list)

    def prompt_block(self) -> str:
        parts = [
            f"## Empresa\n{self.company}",
            f"## Tono y estilo de la marca\n{self.tone}" if self.tone else "",
            f"## Asesor que responde\n{self.agent_name}" if self.agent_name else "",
            f"## Cliente\n{self.customer}",
            f"## Etapa / oportunidades\n{self.stage}" if self.stage else "",
            f"## Productos del catálogo relacionados (únicos precios y disponibilidad que puedes citar)\n{self.catalog}"
            if self.catalog else "## Catálogo\nSin productos relacionados: NO menciones precios ni disponibilidad.",
            f"## Conocimiento y memoria del negocio\n{self.business}" if self.business else "",
            f"## Respuestas rápidas de la empresa\n{self.quick_replies}" if self.quick_replies else "",
            f"## Conversación (más reciente al final)\n{self.transcript}",
        ]
        return "\n\n".join(p for p in parts if p)

    def evidence_text(self) -> str:
        """Texto contra el que se validan cifras: catálogo, conocimiento, respuestas rápidas y la conversación."""
        return "\n".join([self.catalog, self.business, self.quick_replies, self.transcript])


async def history(session: AsyncSession, conv: Conversation, limit: int) -> list[Message]:
    rows = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.sender_type != "system")
        .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).all()
    return list(reversed(rows))


def transcript_of(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        who = SENDER.get(m.sender_type, m.sender_type)
        body = m.text or m.transcript or f"[{m.type}]"
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


async def build(session: AsyncSession, conv: Conversation, agent: Agent | None = None) -> CopilotContext:
    org = conv.organization_id
    cfg = await get_setting(session, "copilot", org)
    msgs = await history(session, conv, int(cfg.get("history_messages") or 20))
    last_in = next((m for m in reversed(msgs) if m.direction == "in"), None)
    company = (await get_setting(session, "company", org)).get("name") or "la empresa"

    ai_agent = None
    agent_id = conv.ai_agent_id or (conv.channel.default_ai_agent_id if conv.channel else None)
    if agent_id:
        ai_agent = await session.get(AIAgent, agent_id)
    tone = (ai_agent.system_prompt or "")[:1500] if ai_agent else ""

    # Cliente (no sensible)
    c = conv.contact
    lines = [f"Nombre: {c.name or 'desconocido'}"]
    golden = await session.get(ContactGolden, c.id)
    gaps = []
    if golden:
        if golden.first_name:
            lines.append(f"Nombres: {golden.first_name} {golden.last_name or ''}".strip())
        if golden.primary_email:
            lines.append(f"Correo: {golden.primary_email}")
        if golden.plates:
            lines.append("Placas: " + ", ".join(golden.plates[:5]))
        for key, label in (("first_name", "nombre"), ("primary_email", "correo"), ("document_number", "documento")):
            if not getattr(golden, key):
                gaps.append(label)
    elif not c.email:
        gaps.append("correo")
    if c.memory:
        lines.append(f"Memoria del cliente: {c.memory[:1200]}")
    if c.stage:
        lines.append(f"Etapa del cliente: {c.stage}")
    products = (await session.scalars(
        select(InteractionProduct).where(InteractionProduct.contact_id == c.id)
        .order_by(InteractionProduct.created_at.desc()).limit(5))).all()
    if products:
        lines.append("Productos de interés: " + "; ".join(f"{p.name} ({p.stage})" for p in products))

    deals = (await session.scalars(select(Deal).where(Deal.contact_id == c.id, Deal.status == "open")
                                   .order_by(Deal.updated_at.desc()).limit(3))).all()
    stage = "; ".join(f"{d.pipeline}: {d.name} → etapa {d.stage}" for d in deals)

    # Negocio: conocimiento del agente + memoria aprobada
    business = ""
    if ai_agent and ai_agent.use_knowledge:
        docs = (await session.scalars(
            select(KnowledgeDoc).join(AIAgentKnowledge, AIAgentKnowledge.doc_id == KnowledgeDoc.id)
            .where(AIAgentKnowledge.ai_agent_id == ai_agent.id, KnowledgeDoc.enabled).order_by(KnowledgeDoc.id))).all()
        business = "\n".join(f"### {d.title}\n{d.content}" for d in docs)[:KB_CHARS]
    memory = (await session.scalars(select(MemoryItem).where(
        MemoryItem.organization_id == org, MemoryItem.status == "approved").order_by(MemoryItem.id).limit(30))).all()
    if memory:
        business += "\n" + "\n".join(f"- {m.title}: {m.content}" for m in memory)
    if last_in and (last_in.text or last_in.transcript):  # fragmentos de la base de conocimiento con RAG
        try:
            from app.knowledge import retrieve

            query = (last_in.text or last_in.transcript or "")[:1000]
            if retrieve.is_question(query) and await retrieve.has_index(session, org):
                async with session.begin_nested():
                    kb = await retrieve.search(session, org, query, limit=4, conversation_id=conv.id,
                                               ai_agent_id=ai_agent.id if ai_agent else None,
                                               agent_id=agent.id if agent else None)
                if kb.passages:
                    business += "\n" + kb.prompt_block(3000, footer=False)
        except Exception:  # noqa: BLE001 (el copiloto responde igual sin RAG)
            log.warning("RAG del copiloto falló", exc_info=True)

    # Catálogo relacionado con lo último que dijo el cliente
    catalog_lines, has_catalog = [], False
    if last_in and (last_in.text or last_in.transcript):
        from app import catalog

        found = await catalog.search(session, org, (last_in.text or last_in.transcript)[:200], limit=5)
        catalog_lines = [catalog.describe(p) for p in found]
        has_catalog = bool(found)

    qrs = (await session.scalars(select(QuickReply).where(
        QuickReply.organization_id == org, QuickReply.is_active).order_by(QuickReply.usage_count.desc()).limit(15))).all()
    quick = "\n".join(f"/{q.shortcut}: {q.text[:200]}" for q in qrs
                      if not q.group_ids or (conv.group_id in (q.group_ids or [])))

    return CopilotContext(
        conversation=conv, company=company, agent_name=agent.name if agent else None, tone=tone,
        transcript=transcript_of(msgs), last_customer_text=(last_in.text or last_in.transcript or "") if last_in else "",
        last_customer_message_id=last_in.id if last_in else None, customer="\n".join(lines),
        business=business.strip(), catalog="\n".join(catalog_lines), has_catalog=has_catalog, quick_replies=quick,
        stage=stage, golden_gaps=gaps)
