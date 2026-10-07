"""Conexiones a LLMs y Cortex (enrutamiento con failover), salud y registro de llamadas."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import CallContext, CortexUnavailable, complete_json
from app.ai.structured import PROVIDERS
from app.auth import require_admin
from app.db import get_session
from app.models import (
    Agent,
    AICall,
    AIConnection,
    AIConnectionHealth,
    Cortex,
    CortexMember,
)
from app.schemas import UTCDateTime
from app.secrets_vault import delete_secret, put_secret
from app.settings_store import add_revision

router = APIRouter(prefix="/api/ai", tags=["cortex"])

PURPOSES = ("chat", "classification", "learning", "flow", "json_edit", "any")
STRATEGIES = ("failover", "lowest_latency", "weighted")
TEST_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


# --- Esquemas -------------------------------------------------------------------
class ConnectionIn(BaseModel):
    name: str
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None  # solo para crear/cambiar; nunca se devuelve
    clear_api_key: bool = False
    default_params: dict = {}
    timeout_ms: int = Field(default=60000, ge=1000, le=600000)
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    is_active: bool = True


class HealthOut(BaseModel):
    state: str = "closed"
    consecutive_failures: int = 0
    last_success_at: UTCDateTime | None = None
    last_failure_at: UTCDateTime | None = None
    last_error: str | None = None
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None


class ConnectionOut(BaseModel):
    id: int
    name: str
    provider: str
    model: str
    base_url: str | None
    has_api_key: bool
    uses_server_key: bool
    default_params: dict
    timeout_ms: int
    input_cost_per_mtok: float | None
    output_cost_per_mtok: float | None
    is_active: bool
    health: HealthOut


class MemberIn(BaseModel):
    connection_id: int
    position: int | None = None
    weight: int = Field(default=1, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1000, le=600000)


class Validation(BaseModel):
    min_chars: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=1)
    banned_phrases: list[str] = []


class CortexIn(BaseModel):
    name: str
    description: str | None = None
    purpose: str = "any"
    strategy: str = "failover"
    max_latency_ms: int | None = Field(default=None, ge=100)
    max_attempts: int = Field(default=3, ge=1, le=10)
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_s: int = Field(default=120, ge=1)
    validation: Validation = Validation()
    is_active: bool = True
    members: list[MemberIn] = []


class MemberOut(BaseModel):
    connection_id: int
    connection_name: str
    provider: str
    model: str
    position: int
    weight: int
    timeout_ms: int | None
    is_active: bool
    health: HealthOut


class CortexOut(BaseModel):
    id: int
    name: str
    description: str | None
    purpose: str
    strategy: str
    max_latency_ms: int | None
    max_attempts: int
    circuit_breaker_failures: int
    circuit_breaker_cooldown_s: int
    validation: dict
    is_active: bool
    members: list[MemberOut]


class CallOut(BaseModel):
    id: int
    created_at: UTCDateTime
    cortex_id: int | None
    connection_id: int | None
    connection_name: str | None
    purpose: str
    conversation_id: int | None
    attempt: int
    status: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None
    fallback_from_call_id: int | None


class TestIn(BaseModel):
    prompt: str = "Responde exactamente: OK"


# --- Helpers --------------------------------------------------------------------
def _health(h: AIConnectionHealth | None) -> HealthOut:
    if not h:
        return HealthOut()
    return HealthOut(state=h.state, consecutive_failures=h.consecutive_failures, last_success_at=h.last_success_at,
                     last_failure_at=h.last_failure_at, last_error=h.last_error, latency_p50_ms=h.latency_p50_ms,
                     latency_p95_ms=h.latency_p95_ms)


async def _healths(session: AsyncSession, ids: list[int]) -> dict[int, AIConnectionHealth]:
    rows = (await session.scalars(select(AIConnectionHealth).where(AIConnectionHealth.connection_id.in_(ids or [0])))).all()
    return {h.connection_id: h for h in rows}


def _conn_out(c: AIConnection, h: AIConnectionHealth | None) -> ConnectionOut:
    return ConnectionOut(
        id=c.id, name=c.name, provider=c.provider, model=c.model, base_url=c.base_url,
        has_api_key=bool(c.api_key_secret_id), uses_server_key=not c.api_key_secret_id and c.provider in ("anthropic", "openai"),
        default_params=c.default_params or {}, timeout_ms=c.timeout_ms,
        input_cost_per_mtok=float(c.input_cost_per_mtok) if c.input_cost_per_mtok is not None else None,
        output_cost_per_mtok=float(c.output_cost_per_mtok) if c.output_cost_per_mtok is not None else None,
        is_active=c.is_active, health=_health(h))


async def cortex_out(session: AsyncSession, cx: Cortex) -> CortexOut:
    await session.refresh(cx, ["members"])
    healths = await _healths(session, [m.connection_id for m in cx.members])
    members = [MemberOut(connection_id=m.connection_id, connection_name=m.connection.name,
                         provider=m.connection.provider, model=m.connection.model, position=m.position,
                         weight=m.weight, timeout_ms=m.timeout_ms, is_active=m.connection.is_active,
                         health=_health(healths.get(m.connection_id)))
               for m in sorted(cx.members, key=lambda m: m.position)]
    return CortexOut(id=cx.id, name=cx.name, description=cx.description, purpose=cx.purpose, strategy=cx.strategy,
                     max_latency_ms=cx.max_latency_ms, max_attempts=cx.max_attempts,
                     circuit_breaker_failures=cx.circuit_breaker_failures,
                     circuit_breaker_cooldown_s=cx.circuit_breaker_cooldown_s, validation=cx.validation or {},
                     is_active=cx.is_active, members=members)


def cortex_document(out: CortexOut) -> dict:
    """Documento JSON editable del Cortex (sin salud ni nombres derivados)."""
    d = out.model_dump(exclude={"id", "members"})
    d["members"] = [{"connection_id": m.connection_id, "position": m.position, "weight": m.weight,
                     "timeout_ms": m.timeout_ms} for m in out.members]
    return d


def _validate_connection(body: ConnectionIn) -> None:
    if body.provider not in PROVIDERS:
        raise HTTPException(422, f"Proveedor inválido: {', '.join(PROVIDERS)}")
    if not body.name.strip() or not body.model.strip():
        raise HTTPException(422, "Nombre y modelo son obligatorios")
    if body.provider in ("openai_compatible", "azure_openai") and not (body.base_url or "").startswith("http"):
        raise HTTPException(422, "Indica la URL base del endpoint (https://...)")


async def _connection(session: AsyncSession, cid: int, org: int) -> AIConnection:
    c = await session.get(AIConnection, cid)
    if not c or c.organization_id != org:
        raise HTTPException(404, "Conexión no encontrada")
    return c


async def _cortex(session: AsyncSession, cid: int, org: int) -> Cortex:
    cx = await session.get(Cortex, cid)
    if not cx or cx.organization_id != org:
        raise HTTPException(404, "Cortex no encontrado")
    return cx


async def apply_cortex(session: AsyncSession, cx: Cortex, body: CortexIn, org: int) -> None:
    """Valida y aplica un documento de Cortex (lo usan la API y la edición de JSON con IA)."""
    if body.purpose not in PURPOSES:
        raise HTTPException(422, f"Propósito inválido: {', '.join(PURPOSES)}")
    if body.strategy not in STRATEGIES:
        raise HTTPException(422, f"Estrategia inválida: {', '.join(STRATEGIES)}")
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")
    dup = await session.scalar(select(Cortex.id).where(Cortex.organization_id == org, Cortex.name == body.name.strip()))
    if dup and dup != cx.id:
        raise HTTPException(409, "Ya existe un Cortex con ese nombre")
    v = body.validation
    if v.min_chars and v.max_chars and v.min_chars > v.max_chars:
        raise HTTPException(422, "min_chars no puede ser mayor que max_chars")
    ids = [m.connection_id for m in body.members]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Una conexión no puede repetirse en el mismo Cortex")
    valid = set((await session.scalars(select(AIConnection.id).where(
        AIConnection.organization_id == org, AIConnection.id.in_(ids or [0])))).all())
    if set(ids) - valid:
        raise HTTPException(422, "Hay conexiones que no existen")

    cx.name, cx.description, cx.purpose, cx.strategy = body.name.strip(), body.description, body.purpose, body.strategy
    cx.max_latency_ms, cx.max_attempts = body.max_latency_ms, body.max_attempts
    cx.circuit_breaker_failures, cx.circuit_breaker_cooldown_s = (body.circuit_breaker_failures,
                                                                  body.circuit_breaker_cooldown_s)
    cx.validation = {k: val for k, val in v.model_dump().items() if val not in (None, [], "")}
    cx.is_active = body.is_active
    if cx.id is None:
        session.add(cx)
        await session.flush()
    await session.execute(delete(CortexMember).where(CortexMember.cortex_id == cx.id))
    for i, m in enumerate(sorted(body.members, key=lambda m: (m.position or 10_000)), start=1):
        session.add(CortexMember(cortex_id=cx.id, connection_id=m.connection_id, position=i, weight=m.weight,
                                 timeout_ms=m.timeout_ms))
    await session.flush()
    session.expire(cx, ["members"])


# --- Conexiones -----------------------------------------------------------------
@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(AIConnection).where(AIConnection.organization_id == agent.organization_id)
                                  .order_by(AIConnection.id))).all()
    healths = await _healths(session, [c.id for c in rows])
    return [_conn_out(c, healths.get(c.id)) for c in rows]


@router.post("/connections", response_model=ConnectionOut)
async def create_connection(body: ConnectionIn, agent: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    _validate_connection(body)
    if await session.scalar(select(AIConnection.id).where(AIConnection.organization_id == agent.organization_id,
                                                          AIConnection.name == body.name.strip())):
        raise HTTPException(409, "Ya existe una conexión con ese nombre")
    c = AIConnection(organization_id=agent.organization_id,
                     **body.model_dump(exclude={"api_key", "clear_api_key", "name"}), name=body.name.strip())
    session.add(c)
    await session.flush()
    if body.api_key:
        c.api_key_secret_id = await put_secret(session, body.api_key, f"ai_connection:{c.id}")
    await session.commit()
    return _conn_out(c, None)


@router.put("/connections/{cid}", response_model=ConnectionOut)
async def update_connection(cid: int, body: ConnectionIn, agent: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    c = await _connection(session, cid, agent.organization_id)
    _validate_connection(body)
    dup = await session.scalar(select(AIConnection.id).where(AIConnection.organization_id == agent.organization_id,
                                                             AIConnection.name == body.name.strip()))
    if dup and dup != c.id:
        raise HTTPException(409, "Ya existe una conexión con ese nombre")
    for k, v in body.model_dump(exclude={"api_key", "clear_api_key"}).items():
        setattr(c, k, v.strip() if k == "name" else v)
    if body.clear_api_key and c.api_key_secret_id:
        await delete_secret(session, c.api_key_secret_id)
        c.api_key_secret_id = None
    elif body.api_key:  # vacío = conservar la clave guardada
        c.api_key_secret_id = await put_secret(session, body.api_key, f"ai_connection:{c.id}", c.api_key_secret_id)
    await session.commit()
    return _conn_out(c, (await _healths(session, [c.id])).get(c.id))


@router.delete("/connections/{cid}")
async def delete_connection(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    c = await _connection(session, cid, agent.organization_id)
    secret = c.api_key_secret_id
    await session.delete(c)  # sale de los Cortex que la usaban (cascade en cortex_members)
    await delete_secret(session, secret)
    await session.commit()
    return {"ok": True}


async def _run_test(session: AsyncSession, cx: Cortex, org: int, prompt: str) -> dict:
    ctx = CallContext(organization_id=org, purpose="test")
    try:
        data = await complete_json(session, cx, "Eres una prueba de conexión. Responde en el esquema JSON.", prompt,
                                   TEST_SCHEMA, ctx, max_tokens=200)
        return {"ok": True, "answer": data.get("answer"), "attempts": ctx.attempts}
    except CortexUnavailable as e:
        return {"ok": False, "error": str(e), "attempts": ctx.attempts}


@router.post("/connections/{cid}/test")
async def test_connection(cid: int, body: TestIn | None = None, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Llamada mínima solo a esta conexión (un Cortex temporal de un miembro; no se guarda)."""
    c = await _connection(session, cid, agent.organization_id)
    temp = Cortex(name=f"prueba:{c.name}", organization_id=c.organization_id, strategy="failover", max_attempts=1,
                  circuit_breaker_failures=10_000, circuit_breaker_cooldown_s=1, validation={}, is_active=True)
    temp.members = [CortexMember(connection_id=c.id, connection=c, position=1, weight=1)]
    return await _run_test(session, temp, agent.organization_id, (body or TestIn()).prompt)


# --- Cortex ---------------------------------------------------------------------
@router.get("/cortexes", response_model=list[CortexOut])
async def list_cortexes(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Cortex).where(Cortex.organization_id == agent.organization_id)
                                  .order_by(Cortex.id))).all()
    return [await cortex_out(session, cx) for cx in rows]


@router.get("/cortexes/{cid}", response_model=CortexOut)
async def get_cortex(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await cortex_out(session, await _cortex(session, cid, agent.organization_id))


@router.post("/cortexes", response_model=CortexOut)
async def create_cortex(body: CortexIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    cx = Cortex(organization_id=agent.organization_id)
    await apply_cortex(session, cx, body, agent.organization_id)
    out = await cortex_out(session, cx)
    await add_revision(session, agent.organization_id, "cortex", cx.id, cortex_document(out), "human", agent.id)
    await session.commit()
    return out


@router.put("/cortexes/{cid}", response_model=CortexOut)
async def update_cortex(cid: int, body: CortexIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    cx = await _cortex(session, cid, agent.organization_id)
    await apply_cortex(session, cx, body, agent.organization_id)
    out = await cortex_out(session, cx)
    await add_revision(session, agent.organization_id, "cortex", cx.id, cortex_document(out), "human", agent.id)
    await session.commit()
    return out


@router.delete("/cortexes/{cid}")
async def delete_cortex(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    cx = await _cortex(session, cid, agent.organization_id)
    await session.delete(cx)  # agentes y ejecuciones que lo usaban quedan con cortex_id = null (usan el principal)
    await session.commit()
    return {"ok": True}


@router.get("/cortexes/{cid}/health", response_model=list[MemberOut])
async def cortex_health(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return (await cortex_out(session, await _cortex(session, cid, agent.organization_id))).members


@router.post("/cortexes/{cid}/test")
async def test_cortex(cid: int, body: TestIn | None = None, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    """Ejecuta una llamada real por el Cortex y muestra cada intento (incluidos los failover)."""
    cx = await _cortex(session, cid, agent.organization_id)
    return await _run_test(session, cx, agent.organization_id, (body or TestIn()).prompt)


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    cortex_id: int | None = None, connection_id: int | None = None, status: str | None = None,
    purpose: str | None = None, conversation_id: int | None = None, before_id: int | None = None,
    limit: int = Query(default=50, le=500),
    agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session),
):
    stmt = select(AICall).where(AICall.organization_id == agent.organization_id)
    for col, val in ((AICall.cortex_id, cortex_id), (AICall.connection_id, connection_id), (AICall.status, status),
                     (AICall.purpose, purpose), (AICall.conversation_id, conversation_id)):
        if val is not None:
            stmt = stmt.where(col == val)
    if before_id:
        stmt = stmt.where(AICall.id < before_id)
    rows = (await session.scalars(stmt.order_by(AICall.id.desc()).limit(limit))).all()
    names = dict((await session.execute(select(AIConnection.id, AIConnection.name).where(
        AIConnection.organization_id == agent.organization_id))).all())
    return [CallOut(id=r.id, created_at=r.created_at, cortex_id=r.cortex_id, connection_id=r.connection_id,
                    connection_name=names.get(r.connection_id), purpose=r.purpose,
                    conversation_id=r.conversation_id, attempt=r.attempt, status=r.status, latency_ms=r.latency_ms,
                    input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                    cost_usd=float(r.cost_usd) if r.cost_usd is not None else None, error=r.error,
                    fallback_from_call_id=r.fallback_from_call_id) for r in rows]
