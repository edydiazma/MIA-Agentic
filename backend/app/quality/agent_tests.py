"""Pruebas de agentes de IA: cada caso simula la conversación con el mismo prompt, conocimiento, memoria y
herramientas que usa el agente en producción, pero en un arenero: no envía mensajes, no agenda citas ni envía
productos (las herramientas con efectos responden «(simulación)»; las de solo lectura consultan los datos reales).

Comprobaciones por caso: must_include / must_not_include (sin mayúsculas ni tildes), expect_handoff,
expect_tool y rubric (un LLM juez puntúa 0-100; pasa con ≥ 70).
"""

import logging
import time
import unicodedata
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import appointments
from app.agent import (
    BOOK_APPOINTMENT,
    CHECK_AVAILABILITY,
    SEARCH_PRODUCTS,
    SEND_PRODUCT,
    build_system,
    handoff_tool,
)
from app.ai import router
from app.ai.base import AgentRequest, TextPart, Turn
from app.ai.router import CallContext, complete_json, resolve_cortex
from app.ai.structured import LLMError
from app.db import SessionLocal
from app.models import (
    AgentTestCase,
    AgentTestResult,
    AgentTestRun,
    AgentTestSuite,
    AIAgent,
    AICall,
    ConfigRevision,
    Group,
    Message,
    Product,
    utcnow,
)

log = logging.getLogger(__name__)
JUDGE_PASS = 70
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["score", "reason"],
                "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}}}


def norm(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", (text or "").lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def turns_from(raw: list) -> list[Turn]:
    turns: list[Turn] = []
    for t in raw or []:
        role = "assistant" if t.get("role") == "assistant" else "user"
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        if turns and turns[-1].role == role:
            turns[-1].parts.append(TextPart(text))
        else:
            turns.append(Turn(role=role, parts=[TextPart(text)]))
    while turns and turns[0].role != "user":
        turns.pop(0)
    while turns and turns[-1].role != "user":
        turns.pop()
    return turns


def clean_turns(raw: list) -> list[dict]:
    out = [{"role": "assistant" if t.get("role") == "assistant" else "user", "text": str(t.get("text") or "")[:4000]}
           for t in raw or [] if isinstance(t, dict) and str(t.get("text") or "").strip()]
    return out[:40]


def clean_expectations(raw: dict | None) -> dict:
    raw = raw or {}
    out: dict = {}
    for k in ("must_include", "must_not_include"):
        items = [str(x).strip()[:200] for x in raw.get(k) or [] if str(x).strip()]
        if items:
            out[k] = items[:20]
    if raw.get("expect_handoff") is not None:
        out["expect_handoff"] = bool(raw["expect_handoff"])
    if str(raw.get("expect_tool") or "").strip():
        out["expect_tool"] = str(raw["expect_tool"]).strip()
    if str(raw.get("rubric") or "").strip():
        out["rubric"] = str(raw["rubric"]).strip()[:2000]
    return out


async def turns_from_conversation(session: AsyncSession, conversation_id: int, limit: int = 30) -> list[dict]:
    """Turnos de una conversación real hasta el último mensaje del cliente (lo que el agente debe responder)."""
    msgs = list(reversed((await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id, Message.sender_type != "system")
        .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).all()))
    turns = [{"role": "user" if m.direction == "in" else "assistant",
              "text": (m.transcript if m.type == "audio" and m.transcript else m.text) or f"[{m.type}]"}
             for m in msgs]
    while turns and turns[-1]["role"] != "user":
        turns.pop()
    return clean_turns(turns)


async def _tools(session: AsyncSession, agent: AIAgent) -> tuple[list, dict]:
    org = agent.organization_id
    groups = {g.name.lower(): g.id for g in (await session.scalars(
        select(Group).where(Group.organization_id == org))).all()}
    appt_cfg, _tz = await appointments.config(session, org)
    tools = [handoff_tool(list(groups))]
    if appt_cfg["enabled"] and agent.use_appointments:
        tools += [CHECK_AVAILABILITY, BOOK_APPOINTMENT]
    if agent.use_catalog and await session.scalar(
            select(func.count()).where(Product.organization_id == org, Product.available)):
        tools += [SEARCH_PRODUCTS, SEND_PRODUCT]
    return tools, groups


async def run_case(session: AsyncSession, agent: AIAgent, case: AgentTestCase, run: AgentTestRun) -> AgentTestResult:
    org = agent.organization_id
    tools, _groups = await _tools(session, agent)
    called: list[str] = []
    handoff: dict = {}

    async def sandbox(name: str, args: dict) -> str:
        called.append(name)
        if name == "transfer_to_human":
            handoff["reason"] = args.get("reason", "")
            return "Transferencia registrada (simulación). Un asesor humano continuará la conversación."
        if name == "check_availability":
            slots = await appointments.available_slots(session, org, date.fromisoformat(args["date"]))
            return ("Horarios libres: " + ", ".join(s.strftime("%H:%M") for s in slots)) if slots else \
                "No hay horarios libres ese día. Sugiere otra fecha."
        if name == "book_appointment":
            return f"Cita agendada para el {args.get('date')} a las {args.get('time')} (simulación)."
        if name == "search_products":
            from app import catalog

            found = await catalog.search(session, org, args.get("query") or "", max_price=args.get("max_price") or None,
                                         category=args.get("category") or None, limit=5)
            return "\n".join(catalog.describe(p) for p in found) or "No hay productos que coincidan."
        if name == "send_product":
            return f"Producto {args.get('sku')} enviado al cliente (simulación)."
        raise ValueError(f"Herramienta desconocida: {name}")

    ctx = CallContext(organization_id=org, purpose="agent_test", ai_agent_id=agent.id)
    result = AgentTestResult(run_id=run.id, case_id=case.id, passed=False, checks=[])
    turns = turns_from(case.turns)
    started = time.monotonic()
    if not turns:
        result.checks = [{"check": "turns", "passed": False, "detail": "El caso no tiene mensajes del cliente"}]
        return result
    try:
        cx = await resolve_cortex(session, org, agent.cortex_id, "chat")
        req = AgentRequest(model="", system=await build_system(session, agent),
                           context="## Contexto\nConversación de prueba (simulación): no hay datos reales del cliente.",
                           turns=turns, tools=tools)
        reply = await router.run_chat(session, cx, req, sandbox, ctx)
        result.reply = reply.text or ""
    except LLMError as e:
        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.checks = [{"check": "error", "passed": False, "detail": str(e)[:500]}]
        result.ai_call_ids = list(ctx.call_ids)
        return result
    result.latency_ms = int((time.monotonic() - started) * 1000)

    exp = case.expectations or {}
    reply_n = norm(result.reply)
    checks = []
    for phrase in exp.get("must_include") or []:
        checks.append({"check": "must_include", "passed": norm(phrase) in reply_n, "detail": phrase})
    for phrase in exp.get("must_not_include") or []:
        checks.append({"check": "must_not_include", "passed": norm(phrase) not in reply_n, "detail": phrase})
    if exp.get("expect_handoff") is not None:
        did = bool(handoff)
        checks.append({"check": "expect_handoff", "passed": did == bool(exp["expect_handoff"]),
                       "detail": "transfirió" if did else "no transfirió"})
    if exp.get("expect_tool"):
        checks.append({"check": "expect_tool", "passed": exp["expect_tool"] in called,
                       "detail": ", ".join(called) or "ninguna herramienta"})
    if exp.get("rubric"):
        try:
            jcx = await resolve_cortex(session, org, None, "agent_test")
            transcript = "\n".join(f"{'Cliente' if t.role == 'user' else 'Agente'}: "
                                   + " ".join(getattr(p, "text", "") for p in t.parts) for t in turns)
            data = await complete_json(
                session, jcx,
                "Eres juez de calidad de un asistente de atención al cliente. Puntúa de 0 a 100 qué tan bien la "
                "RESPUESTA cumple el criterio dado (100 = lo cumple totalmente). Sé estricto y breve en la razón.",
                f"Criterio: {exp['rubric']}\n\nConversación:\n{transcript}\n\nRESPUESTA del agente:\n{result.reply}"
                f"{' [transfirió a un asesor]' if handoff else ''}",
                JUDGE_SCHEMA, ctx, max_tokens=800)
            score = max(0, min(100, int(data.get("score", 0))))
            result.judge_score = score
            checks.append({"check": "rubric", "passed": score >= JUDGE_PASS,
                           "detail": f"{score}/100 — {str(data.get('reason') or '')[:300]}"})
        except (LLMError, TypeError, ValueError) as e:
            checks.append({"check": "rubric", "passed": False, "detail": f"Juez no disponible: {str(e)[:200]}"})
    if not checks:  # sin expectativas: pasa si respondió o transfirió
        checks.append({"check": "responded", "passed": bool(result.reply or handoff), "detail": ""})
    result.checks = checks
    result.passed = all(c["passed"] for c in checks)
    result.ai_call_ids = list(ctx.call_ids)
    return result


async def start_run(session: AsyncSession, suite: AgentTestSuite, trigger: str, started_by: int | None) -> AgentTestRun:
    rev = await session.scalar(select(ConfigRevision.id).where(
        ConfigRevision.organization_id == suite.organization_id, ConfigRevision.entity_type == "ai_agent",
        ConfigRevision.entity_id == str(suite.ai_agent_id)).order_by(ConfigRevision.revision.desc()).limit(1))
    run = AgentTestRun(organization_id=suite.organization_id, suite_id=suite.id, ai_agent_id=suite.ai_agent_id,
                       config_revision_id=rev, trigger=trigger, status="running", started_by=started_by)
    session.add(run)
    await session.commit()
    return run


async def execute_run(run_id: int) -> AgentTestRun:
    """Corre todos los casos del run (sesión propia). Termina en passed | failed | error."""
    async with SessionLocal() as session:
        run = await session.get(AgentTestRun, run_id)
        suite = await session.get(AgentTestSuite, run.suite_id)
        agent = await session.get(AIAgent, suite.ai_agent_id)
        cases = (await session.scalars(select(AgentTestCase).where(AgentTestCase.suite_id == suite.id)
                                       .order_by(AgentTestCase.position, AgentTestCase.id))).all()
        call_ids: list[int] = []
        try:
            if not agent:
                raise ValueError("El agente de IA ya no existe")
            for case in cases:
                res = await run_case(session, agent, case, run)
                session.add(res)
                call_ids += res.ai_call_ids or []
                run.total += 1
                run.passed += int(res.passed)
                await session.commit()
            run.pass_pct = round(100 * run.passed / run.total, 2) if run.total else None
            run.status = "passed" if run.total and run.pass_pct >= suite.min_pass_pct else "failed"
        except Exception as e:
            log.exception("Prueba del agente %s", suite.ai_agent_id)
            await session.rollback()
            run = await session.get(AgentTestRun, run_id)
            run.status = "error"
            session.add(AgentTestResult(run_id=run.id, passed=False,
                                        checks=[{"check": "error", "passed": False, "detail": str(e)[:500]}]))
        if call_ids:
            run.cost_usd = float(await session.scalar(
                select(func.coalesce(func.sum(AICall.cost_usd), 0)).where(AICall.id.in_(call_ids))) or 0)
        run.finished_at = utcnow()
        await session.commit()
        await session.refresh(run)
        return run


async def run_suite(suite_id: int, trigger: str = "manual", started_by: int | None = None) -> AgentTestRun:
    async with SessionLocal() as session:
        suite = await session.get(AgentTestSuite, suite_id)
        run = await start_run(session, suite, trigger, started_by)
    return await execute_run(run.id)
