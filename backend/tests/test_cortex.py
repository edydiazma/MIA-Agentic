"""Cortex: failover por error, latencia y respuesta fuera de rango; circuit breaker; herramientas idempotentes;
registro en ai_calls y salud; API de conexiones y Cortex."""

import asyncio
import itertools
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.ai import router as ai_router
from app.ai.base import AgentRequest, AgentResult, TextPart, Turn, Usage
from app.ai.structured import LLMInvalid
from app.db import SessionLocal
from app.models import AICall, AIConnection, AIConnectionHealth, Cortex, CortexMember, utcnow

REAL_RUN_CHAT = ai_router.run_chat  # el conftest reemplaza run_chat por un fake; aquí probamos el real
ORG = 1
_ids = itertools.count(1)
_conv_ids = itertools.count(910_000)


class FakeProvider:
    """El comportamiento depende del modelo de la conexión."""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, req: AgentRequest, execute_tool) -> AgentResult:
        model = req.model
        if model.startswith("fail"):
            raise RuntimeError(f"caída de {model}")
        if model.startswith("slow:"):
            await asyncio.sleep(float(model.split(":")[1]))
        if model.startswith("banned"):
            return AgentResult(text="Como modelo de lenguaje no puedo ayudarte", usage=Usage(10, 5))
        if model.startswith("tool"):
            await execute_tool("book_appointment", {"date": "2026-10-10", "time": "08:00"})
            if model.startswith("tool-fail"):
                raise RuntimeError("se cayó después de usar la herramienta")
        return AgentResult(text=f"respuesta de {model}", usage=Usage(100, 20, 50))


async def fake_complete_json(conn, system, user, schema, max_tokens=4000):
    if conn.model.startswith("fail"):
        raise RuntimeError("caída")
    if conn.model.startswith("badjson"):
        raise LLMInvalid("JSON inválido")
    if conn.model.startswith("slow:"):
        await asyncio.sleep(float(conn.model.split(":")[1]))
    return {"answer": f"ok {conn.model}"}, Usage(10, 2)


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    monkeypatch.setattr(ai_router, "AnthropicProvider", FakeProvider)
    monkeypatch.setattr(ai_router, "OpenAIProvider", FakeProvider)
    monkeypatch.setattr(ai_router, "_complete_json", fake_complete_json)


async def make_cortex(session, models: list[str], **opts) -> Cortex:
    n = next(_ids)
    cx = Cortex(organization_id=ORG, name=f"cx-{n}", strategy=opts.pop("strategy", "failover"),
                max_latency_ms=opts.pop("max_latency_ms", None), max_attempts=opts.pop("max_attempts", 3),
                circuit_breaker_failures=opts.pop("circuit_breaker_failures", 5),
                circuit_breaker_cooldown_s=opts.pop("circuit_breaker_cooldown_s", 60),
                validation=opts.pop("validation", {}), purpose="any")
    session.add(cx)
    await session.flush()
    for pos, model in enumerate(models, start=1):
        conn = AIConnection(organization_id=ORG, name=f"c{n}-{pos}-{model}", provider="anthropic", model=model,
                            timeout_ms=5000, input_cost_per_mtok=4, output_cost_per_mtok=20)
        session.add(conn)
        await session.flush()
        session.add(CortexMember(cortex_id=cx.id, connection_id=conn.id, position=pos))
    await session.commit()
    await session.refresh(cx, ["members"])
    return cx


def chat_request() -> AgentRequest:
    return AgentRequest(model="", system="Eres un vendedor", turns=[Turn("user", [TextPart("hola")])], tools=[])


async def calls_for(session, conv_id: int) -> list[AICall]:
    return list((await session.scalars(select(AICall).where(AICall.conversation_id == conv_id)
                                       .order_by(AICall.id))).all())


async def run(session, cx, tool=None, conv_id=None):
    ctx = ai_router.CallContext(organization_id=ORG, purpose="chat", conversation_id=conv_id or next(_conv_ids))

    async def default_tool(name, args):
        return "ok"

    result = await REAL_RUN_CHAT(session, cx, chat_request(), tool or default_tool, ctx)
    return result, ctx


async def test_failover_on_error_records_chain_and_cost():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["fail-a", "ok-b"])
        result, ctx = await run(s, cx)
        assert result.text == "respuesta de ok-b"
        rows = await calls_for(s, ctx.conversation_id)
        assert [r.status for r in rows] == ["error", "ok"]
        assert rows[1].fallback_from_call_id == rows[0].id
        assert rows[1].input_tokens == 100 and rows[1].cache_read_tokens == 50
        assert float(rows[1].cost_usd) == pytest.approx((100 * 4 + 20 * 20) / 1_000_000)
        health = await s.get(AIConnectionHealth, rows[1].connection_id)
        assert health.state == "closed" and health.latency_p50_ms is not None
        assert [a["status"] for a in ctx.attempts] == ["error", "ok"]


async def test_latency_budget_triggers_failover_but_last_attempt_waits():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["slow:0.6", "ok-b"], max_latency_ms=150)
        result, ctx = await run(s, cx)
        assert result.text == "respuesta de ok-b"
        rows = await calls_for(s, ctx.conversation_id)
        assert [r.status for r in rows] == ["slow", "ok"]
        assert rows[0].latency_ms < 600  # se cortó por el presupuesto, no esperó la respuesta lenta

        # Un solo miembro: el último intento usa el timeout completo y sí responde
        solo = await make_cortex(s, ["slow:0.3"], max_latency_ms=100)
        result, ctx = await run(s, solo)
        assert result.text == "respuesta de slow:0.3"
        assert [r.status for r in await calls_for(s, ctx.conversation_id)] == ["ok"]


async def test_out_of_range_response_goes_to_next_connection():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["banned-a", "ok-b"],
                               validation={"banned_phrases": ["como modelo de lenguaje"], "min_chars": 3})
        result, ctx = await run(s, cx)
        assert result.text == "respuesta de ok-b"
        rows = await calls_for(s, ctx.conversation_id)
        assert [r.status for r in rows] == ["invalid", "ok"]
        assert "frase prohibida" in rows[0].error

        # complete_json: JSON inválido -> siguiente conexión
        cx2 = await make_cortex(s, ["badjson-a", "ok-b"])
        ctx2 = ai_router.CallContext(organization_id=ORG, purpose="classification", conversation_id=next(_conv_ids))
        data = await ai_router.complete_json(s, cx2, "sys", "user", {"type": "object"}, ctx2)
        assert data == {"answer": "ok ok-b"}
        assert [a["status"] for a in ctx2.attempts] == ["invalid", "ok"]


async def test_all_fail_raises_cortex_unavailable():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["fail-a", "fail-b"])
        with pytest.raises(ai_router.CortexUnavailable) as err:
            await run(s, cx)
        assert len(err.value.attempts) == 2


async def test_circuit_breaker_opens_skips_and_half_opens():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["fail-a", "ok-b"], circuit_breaker_failures=2, circuit_breaker_cooldown_s=60)
        failing_id = cx.members[0].connection_id
        for _ in range(2):
            await run(s, cx)
        health = await s.get(AIConnectionHealth, failing_id)
        await s.refresh(health)
        assert health.state == "open" and health.consecutive_failures == 2

        _, ctx = await run(s, cx)  # abierto: no se intenta
        assert [r.connection_id for r in await calls_for(s, ctx.conversation_id)] == [cx.members[1].connection_id]

        health.opened_at = utcnow() - timedelta(seconds=61)  # pasó el enfriamiento: half-open
        await s.commit()
        _, ctx = await run(s, cx)
        assert [r.connection_id for r in await calls_for(s, ctx.conversation_id)] == [
            failing_id, cx.members[1].connection_id]


async def test_tools_are_idempotent_across_failover():
    executed = []

    async def tool(name, args):
        executed.append((name, args))
        return "Cita agendada"

    async with SessionLocal() as s:
        cx = await make_cortex(s, ["tool-fail-a", "tool-ok-b"])
        result, _ = await run(s, cx, tool=tool)
        assert result.text == "respuesta de tool-ok-b"
        assert len(executed) == 1  # la cita no se agenda dos veces


async def test_lowest_latency_strategy_prefers_fastest():
    async with SessionLocal() as s:
        cx = await make_cortex(s, ["ok-slowish", "ok-fast"], strategy="lowest_latency")
        slow_id, fast_id = cx.members[0].connection_id, cx.members[1].connection_id
        s.add_all([AIConnectionHealth(connection_id=slow_id, latency_p50_ms=900),
                   AIConnectionHealth(connection_id=fast_id, latency_p50_ms=50)])
        await s.commit()
        _, ctx = await run(s, cx)
        assert [r.connection_id for r in await calls_for(s, ctx.conversation_id)] == [fast_id]


# --- API ------------------------------------------------------------------------
async def test_connections_and_cortex_api(client):
    c = client
    r = await c.post("/api/ai/connections", json={
        "name": "Claude propia", "provider": "anthropic", "model": "ok-api", "api_key": "sk-ant-secreta",
        "input_cost_per_mtok": 4, "output_cost_per_mtok": 20})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["has_api_key"] is True and "api_key" not in conn
    async with SessionLocal() as s:
        secret = await s.scalar(text("select decrypted_secret from vault.decrypted_secrets where name = :n"),
                                {"n": f"ai_connection:{conn['id']}"})
    assert secret == "sk-ant-secreta"

    bad = await c.post("/api/ai/connections", json={"name": "x", "provider": "openai_compatible", "model": "m"})
    assert bad.status_code == 422
    backup = (await c.post("/api/ai/connections", json={
        "name": "Respaldo", "provider": "openai_compatible", "model": "fail-api",
        "base_url": "https://llm.example.com/v1"})).json()

    # Actualizar sin clave conserva la guardada
    r = await c.put(f"/api/ai/connections/{conn['id']}", json={
        "name": "Claude propia", "provider": "anthropic", "model": "ok-api", "timeout_ms": 30000})
    assert r.json()["has_api_key"] is True and r.json()["timeout_ms"] == 30000

    body = {"name": "Ventas", "purpose": "chat", "strategy": "failover", "max_latency_ms": 2000,
            "validation": {"banned_phrases": ["no puedo"]},
            "members": [{"connection_id": conn["id"], "position": 2}, {"connection_id": backup["id"], "position": 1}]}
    r = await c.post("/api/ai/cortexes", json=body)
    assert r.status_code == 200, r.text
    cx = r.json()
    assert [m["connection_id"] for m in cx["members"]] == [backup["id"], conn["id"]]
    assert cx["validation"] == {"banned_phrases": ["no puedo"]}
    dup = {**body, "name": "Otro", "members": [{"connection_id": conn["id"]}, {"connection_id": conn["id"]}]}
    assert (await c.post("/api/ai/cortexes", json=dup)).status_code == 422

    # Prueba real por el Cortex: la primera conexión falla y responde la segunda
    r = await c.post(f"/api/ai/cortexes/{cx['id']}/test")
    assert r.json()["ok"] is True and r.json()["answer"] == "ok ok-api"
    assert [a["status"] for a in r.json()["attempts"]] == ["error", "ok"]

    r = await c.post(f"/api/ai/connections/{backup['id']}/test")
    assert r.json()["ok"] is False and r.json()["attempts"][0]["status"] == "error"

    calls = (await c.get("/api/ai/calls", params={"cortex_id": cx["id"]})).json()
    assert [x["status"] for x in calls][:2] == ["ok", "error"] and calls[0]["fallback_from_call_id"] == calls[1]["id"]
    health = (await c.get(f"/api/ai/cortexes/{cx['id']}/health")).json()
    assert health[0]["health"]["consecutive_failures"] >= 1

    revs = (await c.get("/api/revisions", params={"entity_type": "cortex", "entity_id": cx["id"]})).json()
    assert revs and revs[0]["source"] == "human"

    assert (await c.delete(f"/api/ai/connections/{backup['id']}")).json() == {"ok": True}
    assert [m["connection_id"] for m in (await c.get(f"/api/ai/cortexes/{cx['id']}")).json()["members"]] == [conn["id"]]
