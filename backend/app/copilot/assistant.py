"""Asistente del supervisor: preguntas en lenguaje natural sobre la operación, respondidas con herramientas que
llaman a las mismas funciones de los reportes del panel (con el alcance del usuario: un supervisor solo ve sus
grupos). Sin SQL libre. Cada respuesta puede traer datos para graficar (series diarias de los reportes).
"""

import json
import logging
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AgentRequest, TextPart, ToolSpec, Turn
from app.copilot import llm
from app.models import Agent, AssistantMessage, AssistantThread, utcnow
from app.plans import has_feature
from app.scope import _role_scope, agent_allowed, group_allowed, scope_for
from app.settings_store import get_setting

log = logging.getLogger(__name__)
MAX_TOOL_CHARS = 9000
HISTORY = 20

RANGE = {
    "start": {"type": "string", "description": "Fecha inicial AAAA-MM-DD (por defecto hace 7 días)"},
    "end": {"type": "string", "description": "Fecha final AAAA-MM-DD (por defecto hoy)"},
}


def _tool(name: str, description: str, extra: dict | None = None) -> ToolSpec:
    return ToolSpec(name=name, description=description,
                    schema={"type": "object", "properties": {**RANGE, **(extra or {})}, "additionalProperties": False})


TOOLS = [
    _tool("service_kpis", "Nivel de servicio: casos, atendidas, abandonadas, AHT, ASA, tasa de atención y de abandono, "
          "por grupo y por asesor.", {"group_id": {"type": "integer"}, "agent_id": {"type": "integer"}}),
    ToolSpec("realtime", "Estado actual: conversaciones abiertas, en cola, asesores en línea, contadores de hoy.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    _tool("agents_performance", "Desempeño por asesor: cerradas, ventas, mensajes, primera respuesta, SLA."),
    _tool("login_report", "Tiempo por estado de cada asesor y horas trabajadas.", {"agent_id": {"type": "integer"}}),
    _tool("general_report", "Reporte general: conversaciones nuevas, transferidas, cerradas, ventas, mensajes."),
    _tool("quality", "Calidad (QA): puntaje por asesor y del bot, fallas críticas, sentimiento."),
    _tool("ads_performance", "Anuncios: inversión, conversaciones, clientes nuevos, ventas, CPL, CPA, ROAS por anuncio.",
          {"platform": {"type": "string", "enum": ["meta", "google_ads"]}}),
    _tool("channels", "Conversaciones y mensajes por canal (WhatsApp, Instagram, Messenger, chat web, correo)."),
    _tool("customers", "Clientes: nuevos, activos, recurrentes, recencia, antigüedad."),
    _tool("products", "Productos más pedidos: mencionados, cotizados, comprados y conversión."),
    _tool("copilot_adoption", "Uso del copiloto por asesor: sugerencias mostradas, aceptadas, editadas, descartadas."),
]

SYSTEM = (
    "Eres el asistente de operación del contact center de {company}. Respondes preguntas de supervisores sobre "
    "atención, asesores, calidad, anuncios, canales, clientes y productos usando SOLO las herramientas: nunca "
    "inventes cifras. Fecha de hoy: {today}. Si la pregunta no indica periodo, usa los últimos 7 días y dilo. "
    "Responde en español, breve: primero la respuesta directa con los números clave, luego 2–4 viñetas con hallazgos "
    "y, si aplica, una recomendación accionable. Si una herramienta devuelve un error de alcance, explícalo.")


def _parse(d: str | None, default: date) -> date:
    try:
        return date.fromisoformat(d) if d else default
    except ValueError:
        return default


def can_use(agent: Agent) -> bool:
    return agent.role in ("admin", "supervisor")


async def require_access(session: AsyncSession, agent: Agent) -> None:
    if not (can_use(agent) or await _role_scope(session, agent) in ("all", "groups")):
        raise HTTPException(403, "El asistente es para supervisores y administradores")
    if not (await get_setting(session, "copilot", agent.organization_id)).get("assistant", True):
        raise HTTPException(403, "El asistente está desactivado en Configuraciones → Copiloto")


def _compact(value) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + "…(recortado)"


async def run_tool(session: AsyncSession, agent: Agent, name: str, args: dict) -> tuple[dict | list, dict | None]:
    """(resultado, datos para gráfico). Los reportes aplican el alcance del usuario por dentro."""
    today = date.today()
    start, end = _parse(args.get("start"), today - timedelta(days=6)), _parse(args.get("end"), today)
    org = agent.organization_id
    scope = await scope_for(session, agent)
    from app.routers import customer_reports, reports

    if args.get("agent_id") and not agent_allowed(scope, args["agent_id"]):
        return {"error": "Ese asesor está fuera de tu alcance"}, None
    if name == "service_kpis":
        gid = args.get("group_id")
        if gid and not group_allowed(scope, gid):
            return {"error": "Ese grupo está fuera de tu alcance"}, None
        data = await reports.service_report(start=start, end=end, group_id=gid, agent_id=args.get("agent_id"),
                                            agent=agent, session=session)
    elif name == "realtime":
        data = await reports.realtime(agent=agent, session=session)
    elif name == "agents_performance":
        data = await reports.agents_report(start=start, end=end, agent=agent, session=session)
    elif name == "login_report":
        data = await reports.login_report(start=start, end=end, agent_id=args.get("agent_id"), agent=agent,
                                          session=session)
    elif name == "general_report":
        data = await reports.general(start=start, end=end, agent=agent, session=session)
    elif name == "quality":
        if not await has_feature(session, org, "qa"):
            return {"error": "El plan no incluye Calidad (QA)"}, None
        from app.routers.quality import qa_report

        data = await qa_report(start=start, end=end, agent=agent, session=session)
    elif name == "ads_performance":
        if not await has_feature(session, org, "attribution"):
            return {"error": "El plan no incluye Atribución"}, None
        from app.routers.ads import ads_report

        data = await ads_report(start=start, end=end, platform=args.get("platform"), campaign=None, agent=agent,
                                session=session)
    elif name == "channels":
        from app.routers.channel_reports import channels_report

        data = await channels_report(start=start, end=end, agent=agent, session=session)
    elif name == "customers":
        data = await customer_reports.customers_report(start=start, end=end, agent=agent, session=session)
    elif name == "products":
        data = await customer_reports.products_report(start=start, end=end, agent=agent, session=session)
    elif name == "copilot_adoption":
        from app.routers.copilot import adoption_data

        data = await adoption_data(session, agent, start, end)
    else:
        return {"error": f"Herramienta desconocida: {name}"}, None
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    chart = None
    series = data.get("series") if isinstance(data, dict) else None
    if isinstance(series, list) and series and isinstance(series[0], dict):
        keys = [k for k, v in series[0].items() if k != "day" and isinstance(v, (int, float))][:4]
        if keys:
            chart = {"title": name, "data": series[:62], "keys": keys}
    return data, chart


async def ask(session: AsyncSession, agent: Agent, thread: AssistantThread, question: str) -> list[AssistantMessage]:
    """Guarda la pregunta, conversa con herramientas y guarda las llamadas a herramientas y la respuesta."""
    session.add(AssistantMessage(thread_id=thread.id, role="user", content=question))
    await session.flush()
    previous = (await session.scalars(select(AssistantMessage).where(
        AssistantMessage.thread_id == thread.id, AssistantMessage.role.in_(("user", "assistant")))
        .order_by(AssistantMessage.id.desc()).limit(HISTORY))).all()
    turns = [Turn(role=m.role, parts=[TextPart(m.content or "")]) for m in reversed(previous) if m.content]
    company = (await get_setting(session, "company", agent.organization_id)).get("name") or "la empresa"
    req = AgentRequest(model="", system=SYSTEM.format(company=company, today=date.today().isoformat()),
                       turns=turns, tools=TOOLS, max_tokens=2500,
                       context=f"Usuario: {agent.name} ({agent.role}).")
    calls: list[dict] = []
    charts: list[dict] = []

    async def execute(name: str, args: dict) -> str:
        try:
            data, chart = await run_tool(session, agent, name, args or {})
        except HTTPException as e:
            data, chart = {"error": e.detail}, None
        except Exception as e:  # noqa: BLE001 — el modelo recibe el error y lo explica
            log.exception("Herramienta %s del asistente falló", name)
            data, chart = {"error": f"{type(e).__name__}"}, None
        calls.append({"name": name, "input": args, "ok": not (isinstance(data, dict) and data.get("error"))})
        if chart:
            charts.append(chart)
        return _compact(data)

    result, call_id = await llm.chat(session, agent.organization_id, req, execute)
    new: list[AssistantMessage] = []
    if calls:
        new.append(AssistantMessage(thread_id=thread.id, role="tool", content=None, tool_calls=calls))
    new.append(AssistantMessage(thread_id=thread.id, role="assistant", content=result.text, tool_calls=calls or None,
                                charts=charts or None, ai_call_id=call_id))
    session.add_all(new)
    if not thread.title:
        thread.title = question[:80]
    thread.updated_at = utcnow()
    await session.commit()
    return new
