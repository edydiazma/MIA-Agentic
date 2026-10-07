"""Copiloto del asesor: sugerencias, siguiente acción, borradores, reescritura, resúmenes y adopción. §19.3"""

from collections import Counter, defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.copilot import core, hooks
from app.db import get_session
from app.models import Agent, Conversation
from app.scope import require_conversation, scope_for

router = APIRouter(prefix="/api", tags=["copilot"])


async def _conv(session: AsyncSession, agent: Agent, conv_id: int) -> Conversation:
    return await require_conversation(session, agent, await session.get(Conversation, conv_id))


async def _suggestion(session: AsyncSession, agent: Agent, suggestion_id: int):
    row = await core._get(session, suggestion_id)
    if row is None or row.organization_id != agent.organization_id:
        raise HTTPException(404, "Sugerencia no encontrada")
    conv = await _conv(session, agent, row.conversation_id)
    return row, conv


@router.get("/conversations/{conv_id}/copilot")
async def conversation_copilot(conv_id: int, agent: Agent = Depends(current_agent),
                               session: AsyncSession = Depends(get_session)):
    """Últimas sugerencias, siguiente acción y resúmenes. Si no hay sugerencias frescas para el último mensaje del
    cliente y las sugerencias son automáticas, las genera en segundo plano (llegan por tiempo real)."""
    conv = await _conv(session, agent, conv_id)
    data = await core.overview(session, conv)
    cfg = await core.settings(session, conv.organization_id)
    data["enabled"] = bool(cfg.get("enabled"))
    data["mode"] = cfg.get("suggestions")
    data["pending"] = False
    if (cfg.get("enabled") and cfg.get("suggestions") == "auto" and conv.status == "human"
            and not data["fresh"] and conv.last_inbound_at):
        hooks.on_inbound(conv.id, 0, delay=0)
        data["pending"] = True
    return data


@router.post("/conversations/{conv_id}/copilot/refresh")
async def refresh(conv_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Genera sugerencias ahora (aunque haya en caché). Respeta el doble del límite por hora."""
    conv = await _conv(session, agent, conv_id)
    if conv.status != "human":
        raise HTTPException(409, "El copiloto sugiere respuestas cuando un asesor atiende la conversación")
    return await core.generate(session, conv, force=True, agent=agent)


class TypingIn(BaseModel):
    typing: bool = True


@router.post("/conversations/{conv_id}/copilot/typing")
async def typing(conv_id: int, body: TypingIn, agent: Agent = Depends(current_agent),
                 session: AsyncSession = Depends(get_session)):
    await _conv(session, agent, conv_id)
    if body.typing:
        hooks.agent_typing(conv_id)
    return {"ok": True}


class OutcomeIn(BaseModel):
    status: str
    final_text: str | None = None


@router.post("/copilot/suggestions/{suggestion_id}/outcome")
async def outcome(suggestion_id: int, body: OutcomeIn, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    row, _conv_ = await _suggestion(session, agent, suggestion_id)
    try:
        return await core.record_outcome(session, row, body.status, body.final_text)
    except core.CopilotError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/copilot/suggestions/{suggestion_id}/execute")
async def execute(suggestion_id: int, agent: Agent = Depends(current_agent),
                  session: AsyncSession = Depends(get_session)):
    row, conv = await _suggestion(session, agent, suggestion_id)
    if row.kind != "next_action":
        raise HTTPException(422, "Solo se ejecutan acciones sugeridas")
    from app.interaction_products import ProductError

    try:
        return await core.execute_action(session, row, conv, agent)
    except (core.CopilotError, ProductError) as e:
        raise HTTPException(422, str(e)) from e


class DraftIn(BaseModel):
    instruction: str | None = Field(default=None, max_length=1000)


@router.post("/conversations/{conv_id}/copilot/draft")
async def draft(conv_id: int, body: DraftIn, agent: Agent = Depends(current_agent),
                session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, agent, conv_id)
    return await core.draft(session, conv, agent, body.instruction)


class RewriteIn(BaseModel):
    text: str = Field(max_length=4000)
    mode: str
    target_lang: str | None = None
    conversation_id: int | None = None


@router.post("/copilot/rewrite")
async def rewrite(body: RewriteIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if body.conversation_id:
        await _conv(session, agent, body.conversation_id)
    try:
        return await core.rewrite(session, agent.organization_id, agent, body.text, body.mode, body.target_lang,
                                  body.conversation_id)
    except core.CopilotError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/conversations/{conv_id}/summary")
async def summary(conv_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conv = await _conv(session, agent, conv_id)
    text = await core.conversation_summary(session, conv)
    if text is None:
        raise HTTPException(409, "El copiloto está desactivado o la conversación no tiene mensajes")
    return {"summary": text, "summary_at": conv.summary_at, "handoff_summary": conv.handoff_summary}


# --- Adopción ------------------------------------------------------------------------------------------------
async def adoption_data(session: AsyncSession, agent: Agent, start: date | None, end: date | None) -> dict:
    from app.routers.reports import _days, _pct, _range, _rows

    org = agent.organization_id
    _lo, _hi, _tz, start, end = await _range(session, org, start, end)
    scope = await scope_for(session, agent)
    rows = await _rows(session, """
        select d.day, d.agent_id, d.kind, d.shown, d.accepted, d.edited, d.dismissed, d.latency_sum_ms,
               a.name as agent_name
        from reporting.daily_copilot d left join public.agents a on a.id = d.agent_id
        where d.organization_id = :o and d.day between :a and :b""", o=org, a=start, b=end)
    if not scope.unrestricted:
        allowed = scope.member_ids | {scope.agent_id}
        rows = [r for r in rows if r["agent_id"] in allowed]
    keys = ("shown", "accepted", "edited", "dismissed", "latency_sum_ms")
    totals: Counter = Counter()
    by_agent: dict[int, Counter] = defaultdict(Counter)
    by_kind: dict[str, Counter] = defaultdict(Counter)
    series: dict[str, Counter] = {d.isoformat(): Counter() for d in _days(start, end)}
    names: dict[int, str] = {}
    for r in rows:
        for k in keys:
            v = r[k] or 0
            totals[k] += v
            by_agent[r["agent_id"]][k] += v
            by_kind[r["kind"]][k] += v
            series[r["day"].isoformat()][k] += v
        names[r["agent_id"]] = r["agent_name"] or "Sin asesor"

    def rates(c: Counter) -> dict:
        used = c["accepted"] + c["edited"]
        return {"shown": c["shown"], "accepted": c["accepted"], "edited": c["edited"], "dismissed": c["dismissed"],
                "adoption_pct": _pct(used, c["shown"]), "accepted_unchanged_pct": _pct(c["accepted"], used),
                "avg_latency_ms": round(c["latency_sum_ms"] / c["shown"]) if c["shown"] else None}

    return {
        "totals": rates(totals),
        "by_agent": sorted([{"agent_id": a, "name": names.get(a), **rates(c)} for a, c in by_agent.items()],
                           key=lambda x: -x["shown"]),
        "by_kind": sorted([{"kind": k, **rates(c)} for k, c in by_kind.items()], key=lambda x: -x["shown"]),
        "series": [{"day": d, "shown": c["shown"], "used": c["accepted"] + c["edited"], "dismissed": c["dismissed"]}
                   for d, c in series.items()],
    }


@router.get("/reports/copilot")
async def copilot_report(start: date | None = None, end: date | None = None, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    return await adoption_data(session, agent, start, end)


@router.get("/copilot/status")
async def status(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    cfg = await core.settings(session, agent.organization_id)
    return {k: cfg.get(k) for k in ("enabled", "suggestions", "next_action", "handoff_summary", "close_summary",
                                     "assistant")}


__all__ = ["router", "adoption_data"]
