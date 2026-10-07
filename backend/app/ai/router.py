"""Cortex: enrutamiento de llamadas a LLMs con failover.

Un Cortex agrupa varias conexiones (ai_connections) y decide en qué orden probarlas:
- failover: por posición; lowest_latency: por latencia p50 observada; weighted: aleatorio ponderado.
Pasa a la siguiente conexión cuando la actual:
- falla (error de red, autenticación, límite) o se niega (refusal),
- excede el presupuesto de latencia (max_latency_ms; el último intento usa el timeout completo),
- responde "fuera de rango" según cortex.validation (JSON inválido, longitud, frases prohibidas).
Circuit breaker: tras N fallas seguidas la conexión queda abierta durante X segundos.
Cada intento queda en ai_calls (estado, latencia, tokens, costo y de qué llamada vino el failover).
"""

import asyncio
import json
import logging
import random
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.base import AgentRequest, AgentResult, ToolExecutor, Usage
from app.ai.openai_provider import OpenAIProvider
from app.ai.structured import LLMError, LLMInvalid, LLMRefused, ResolvedConnection
from app.ai.structured import complete_json as _complete_json
from app.config import get_settings
from app.db import SessionLocal
from app.models import AICall, AIConnection, AIConnectionHealth, Cortex, utcnow
from app.secrets_vault import get_secret

log = logging.getLogger(__name__)
_latencies: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=100))


class CortexUnavailable(LLMError):
    """Todas las conexiones del Cortex fallaron o están en pausa."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class CallContext:
    organization_id: int
    purpose: str  # chat | classification | learning | flow | json_edit | test
    conversation_id: int | None = None
    ai_agent_id: int | None = None
    attempts: list[dict] = field(default_factory=list)
    call_ids: list[int] = field(default_factory=list)  # ids exactos en ai_calls (para trazabilidad)


# --- Resolución -----------------------------------------------------------------
async def resolve_cortex(session: AsyncSession, org: int, cortex_id: int | None, purpose: str) -> Cortex:
    """Cortex indicado o, si no hay, el primero activo para ese propósito (o "any")."""
    if cortex_id:
        cx = await session.get(Cortex, cortex_id)
        if cx and cx.organization_id == org and cx.is_active:
            return cx
    stmt = (select(Cortex).where(Cortex.organization_id == org, Cortex.is_active,
                                 Cortex.purpose.in_([purpose, "any"]))
            .order_by((Cortex.purpose == purpose).desc(), Cortex.id))
    cx = (await session.scalars(stmt)).first()
    if not cx:
        raise CortexUnavailable("No hay un Cortex activo configurado", [])
    return cx


async def resolve_connection(session: AsyncSession, conn: AIConnection) -> ResolvedConnection:
    env = get_settings()
    key = await get_secret(session, conn.api_key_secret_id)
    if not key:  # sin clave propia: la del servidor según el proveedor
        key = {"anthropic": env.anthropic_api_key, "openai": env.openai_api_key}.get(conn.provider) or None
    return ResolvedConnection(provider=conn.provider, model=conn.model, api_key=key,
                              base_url=conn.base_url, params=dict(conn.default_params or {}))


# --- Validación "fuera de rango" ------------------------------------------------
def validate_text(text: str, rules: dict) -> str | None:
    """Devuelve el motivo si la respuesta incumple las reglas del Cortex."""
    if not rules:
        return None
    n = len(text or "")
    if rules.get("min_chars") and n < int(rules["min_chars"]):
        return f"respuesta demasiado corta ({n} caracteres)"
    if rules.get("max_chars") and n > int(rules["max_chars"]):
        return f"respuesta demasiado larga ({n} caracteres)"
    low = (text or "").lower()
    for phrase in rules.get("banned_phrases") or []:
        if phrase and phrase.lower() in low:
            return f"contiene una frase prohibida: «{phrase}»"
    return None


# --- Salud y circuit breaker ----------------------------------------------------
async def _health(session: AsyncSession, connection_id: int) -> AIConnectionHealth:
    h = await session.get(AIConnectionHealth, connection_id)
    if not h:
        h = AIConnectionHealth(connection_id=connection_id)
        session.add(h)
    return h


def _available(h: AIConnectionHealth | None, cx: Cortex) -> bool:
    if not h or h.state != "open":
        return True
    # Abierto: se vuelve a probar (half-open) cuando pasa el enfriamiento
    return h.opened_at is not None and utcnow() - h.opened_at >= timedelta(seconds=cx.circuit_breaker_cooldown_s)


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


async def _record(cx: Cortex, conn: AIConnection, ctx: CallContext, attempt: int,
                  status: str, latency_ms: int, usage: Usage | None, error: str | None,
                  fallback_from: int | None) -> int:
    """Registra el intento y la salud en una sesión propia (no toca la transacción del llamador)."""
    async with SessionLocal() as session:
        call_id = await _record_in(session, cx, conn, ctx, attempt, status, latency_ms, usage, error, fallback_from)
        await session.commit()
    ctx.call_ids.append(call_id)
    return call_id


async def _record_in(session: AsyncSession, cx: Cortex, conn: AIConnection, ctx: CallContext, attempt: int,
                     status: str, latency_ms: int, usage: Usage | None, error: str | None,
                     fallback_from: int | None) -> int:
    cost = None
    if usage and (conn.input_cost_per_mtok or conn.output_cost_per_mtok):
        cost = (usage.input_tokens * float(conn.input_cost_per_mtok or 0)
                + usage.output_tokens * float(conn.output_cost_per_mtok or 0)) / 1_000_000
    call = AICall(organization_id=ctx.organization_id, cortex_id=cx.id, connection_id=conn.id, purpose=ctx.purpose,
                  conversation_id=ctx.conversation_id, ai_agent_id=ctx.ai_agent_id, attempt=attempt, status=status,
                  latency_ms=latency_ms, input_tokens=usage.input_tokens if usage else None,
                  output_tokens=usage.output_tokens if usage else None,
                  cache_read_tokens=usage.cache_read_tokens if usage else None, cost_usd=cost,
                  error=(error or "")[:2000] or None, fallback_from_call_id=fallback_from)
    session.add(call)

    h = await _health(session, conn.id)
    now = utcnow()
    if status == "ok":
        _latencies[conn.id].append(latency_ms)
        h.state, h.consecutive_failures, h.last_success_at, h.opened_at = "closed", 0, now, None
        lat = list(_latencies[conn.id])
        h.latency_p50_ms, h.latency_p95_ms = _percentile(lat, 0.5), _percentile(lat, 0.95)
    else:
        h.consecutive_failures = (h.consecutive_failures or 0) + 1
        h.last_failure_at, h.last_error = now, (error or status)[:1000]
        if h.consecutive_failures >= cx.circuit_breaker_failures:
            h.state, h.opened_at = "open", now
    h.updated_at = now
    await session.flush()
    ctx.attempts.append({"connection": conn.name, "model": conn.model, "status": status, "latency_ms": latency_ms,
                         "error": error})
    return call.id


def _order(cx: Cortex, health: dict[int, AIConnectionHealth]) -> list:
    members = [m for m in cx.members if m.connection.is_active]
    if cx.strategy == "lowest_latency":
        return sorted(members, key=lambda m: (health.get(m.connection_id) is None
                                              or health[m.connection_id].latency_p50_ms is None,
                                              getattr(health.get(m.connection_id), "latency_p50_ms", 0) or 0,
                                              m.position))
    if cx.strategy == "weighted":
        pool, ordered = list(members), []
        while pool:
            pick = random.choices(pool, weights=[max(1, m.weight) for m in pool])[0]
            ordered.append(pick)
            pool.remove(pick)
        return ordered
    return sorted(members, key=lambda m: m.position)


# --- Ejecución con failover -----------------------------------------------------
async def _execute(session: AsyncSession, cx: Cortex, ctx: CallContext,
                   attempt_fn: Callable[[ResolvedConnection], Awaitable[tuple[object, Usage]]]) -> object:
    health = {h.connection_id: h for h in (await session.scalars(
        select(AIConnectionHealth).where(AIConnectionHealth.connection_id.in_([m.connection_id for m in cx.members]))
    )).all()}
    candidates = [m for m in _order(cx, health) if _available(health.get(m.connection_id), cx)]
    if not candidates:  # todas en pausa: se intenta igual con la de mejor posición antes que no responder
        candidates = sorted([m for m in cx.members if m.connection.is_active], key=lambda m: m.position)[:1]
    candidates = candidates[: cx.max_attempts]
    if not candidates:
        raise CortexUnavailable(f"El Cortex «{cx.name}» no tiene conexiones activas", ctx.attempts)

    previous_call: int | None = None
    for i, member in enumerate(candidates, start=1):
        conn = member.connection
        last = i == len(candidates)
        timeout_ms = member.timeout_ms or conn.timeout_ms
        budget = cx.max_latency_ms if (cx.max_latency_ms and not last) else None
        effective = min(timeout_ms, budget) if budget else timeout_ms
        resolved = await resolve_connection(session, conn)
        started = time.monotonic()
        usage, status, error, result = None, "ok", None, None
        try:
            result, usage = await asyncio.wait_for(attempt_fn(resolved), timeout=effective / 1000)
        except TimeoutError:
            status = "slow" if budget and effective < timeout_ms else "timeout"
            error = f"sin respuesta en {effective} ms"
        except LLMRefused as e:
            status, error = "refused", str(e)
        except LLMInvalid as e:
            status, error = "invalid", str(e)
        except Exception as e:  # red, autenticación, límites, errores del SDK
            status, error = "error", f"{type(e).__name__}: {e}"
        latency = int((time.monotonic() - started) * 1000)
        previous_call = await _record(cx, conn, ctx, i, status, latency, usage, error, previous_call)
        if status == "ok":
            return result
        log.warning("Cortex «%s»: %s/%s falló (%s), siguiente conexión", cx.name, conn.name, conn.model, status)
    raise CortexUnavailable(f"El Cortex «{cx.name}» no obtuvo respuesta válida en {len(candidates)} intentos",
                            ctx.attempts)


async def run_chat(session: AsyncSession, cx: Cortex, req: AgentRequest, execute_tool: ToolExecutor,
                   ctx: CallContext) -> AgentResult:
    """Conversación con herramientas. Las herramientas son idempotentes entre intentos de failover."""
    cache: dict[str, str] = {}

    async def idempotent_tool(name: str, args: dict) -> str:
        key = name + json.dumps(args, sort_keys=True, default=str)
        if key not in cache:
            cache[key] = await execute_tool(name, args)
        return cache[key]

    async def attempt(conn: ResolvedConnection):
        provider = (AnthropicProvider(conn.api_key, conn.base_url) if conn.provider == "anthropic"
                    else OpenAIProvider(conn.provider, conn.api_key, conn.base_url, conn.params))
        r = AgentRequest(model=conn.model, system=req.system, context=req.context, turns=req.turns, tools=req.tools,
                         effort=req.effort or conn.params.get("effort"),
                         max_tokens=int(conn.params.get("max_tokens") or req.max_tokens))
        result = await provider.run(r, idempotent_tool)
        if result.refused:
            raise LLMRefused("El modelo se negó a responder")
        reason = validate_text(result.text, cx.validation or {})
        if reason:
            raise LLMInvalid(reason)
        return result, result.usage

    return await _execute(session, cx, ctx, attempt)


async def complete_json(session: AsyncSession, cx: Cortex, system: str, user: str, schema: dict, ctx: CallContext,
                        max_tokens: int = 4000) -> dict:
    async def attempt(conn: ResolvedConnection):
        return await _complete_json(conn, system, user, schema, max_tokens=int(conn.params.get("max_tokens") or max_tokens))

    return await _execute(session, cx, ctx, attempt)


async def json_call(org: int, cortex_id: int | None, purpose: str, system: str, user: str, schema: dict,
                    conversation_id: int | None = None, max_tokens: int = 4000) -> tuple[dict, CallContext]:
    """Atajo con sesión propia (los ai_calls quedan registrados aunque el llamador haga rollback)."""
    ctx = CallContext(organization_id=org, purpose=purpose, conversation_id=conversation_id)
    async with SessionLocal() as s:
        cx = await resolve_cortex(s, org, cortex_id, purpose)
        data = await complete_json(s, cx, system, user, schema, ctx, max_tokens)
    return data, ctx
