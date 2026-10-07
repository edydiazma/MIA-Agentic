"""Puerta única a los modelos para el copiloto. Las pruebas reemplazan `call_json` y `chat`."""

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import router as ai_router
from app.ai.base import AgentRequest, AgentResult, ToolExecutor
from app.ai.router import CallContext
from app.db import SessionLocal
from app.settings_store import get_setting

# Función del copiloto → (propósito del Cortex, clave del Cortex propio en la configuración)
FEATURES = {
    "reply": ("copilot", "reply_cortex_id"),
    "draft": ("copilot", "reply_cortex_id"),
    "rewrite": ("copilot", "reply_cortex_id"),
    "summary": ("copilot", "summary_cortex_id"),
    "assistant": ("assistant", "assistant_cortex_id"),
}


async def _cortex_id(session: AsyncSession, org: int, feature: str) -> int | None:
    key = FEATURES[feature][1]
    return (await get_setting(session, "copilot", org)).get(key)


async def call_json(org: int, feature: str, system: str, user: str, schema: dict,
                    conversation_id: int | None = None, max_tokens: int = 1500) -> tuple[dict, int | None, int]:
    """(datos, id del ai_call, latencia ms). Sesión propia: el ai_call queda aunque el llamador haga rollback."""
    purpose = FEATURES[feature][0]
    started = time.monotonic()
    ctx = CallContext(organization_id=org, purpose=purpose, conversation_id=conversation_id)
    async with SessionLocal() as s:
        cx = await ai_router.resolve_cortex(s, org, await _cortex_id(s, org, feature), purpose)
        data = await ai_router.complete_json(s, cx, system, user, schema, ctx, max_tokens)
    call_id = ctx.call_ids[-1] if ctx.call_ids else None
    return data, call_id, int((time.monotonic() - started) * 1000)


async def chat(session: AsyncSession, org: int, req: AgentRequest, execute_tool: ToolExecutor,
               conversation_id: int | None = None) -> tuple[AgentResult, int | None]:
    """Conversación con herramientas (asistente del supervisor)."""
    ctx = CallContext(organization_id=org, purpose="assistant", conversation_id=conversation_id)
    cx = await ai_router.resolve_cortex(session, org, await _cortex_id(session, org, "assistant"), "assistant")
    result = await ai_router.run_chat(session, cx, req, execute_tool, ctx)
    return result, (ctx.call_ids[-1] if ctx.call_ids else None)
