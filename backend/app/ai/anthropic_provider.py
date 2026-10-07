import base64
import logging

import anthropic

from app.ai.base import (
    MAX_TOOL_STEPS,
    AgentRequest,
    AgentResult,
    DocumentPart,
    ImagePart,
    TextPart,
    ToolExecutor,
    Turn,
    Usage,
)

log = logging.getLogger(__name__)

# Modelos que aceptan el fallback del lado del servidor ante un rechazo (refusal).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
# Modelos sin parámetro effort.
NO_EFFORT_MODELS = ("claude-haiku-4-5",)


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def _content(turn: Turn) -> list[dict]:
    blocks: list[dict] = []
    for p in turn.parts:
        if isinstance(p, TextPart):
            blocks.append({"type": "text", "text": p.text})
        elif isinstance(p, ImagePart):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": p.mime, "data": _b64(p.data)}})
        elif isinstance(p, DocumentPart):
            blocks.append({"type": "document", "title": p.filename,
                           "source": {"type": "base64", "media_type": p.mime, "data": _b64(p.data)}})
    return blocks


def client(api_key: str | None, base_url: str | None = None) -> anthropic.AsyncAnthropic:
    kw: dict = {"max_retries": 0}  # los reintentos los decide el Cortex (failover)
    if api_key:
        kw["api_key"] = api_key
    if base_url:
        kw["base_url"] = base_url
    return anthropic.AsyncAnthropic(**kw)


def common_kwargs(model: str, effort: str | None) -> dict:
    kwargs: dict = {}
    if effort and not model.startswith(NO_EFFORT_MODELS):
        kwargs["output_config"] = {"effort": effort}
    if model.startswith(FALLBACK_MODELS):
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
    return kwargs


def add_usage(usage: Usage, resp) -> None:
    u = getattr(resp, "usage", None)
    if u:
        usage.add(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0)


class AnthropicProvider:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.client = client(api_key, base_url)

    async def run(self, req: AgentRequest, execute_tool: ToolExecutor) -> AgentResult:
        messages: list[dict] = [{"role": t.role, "content": _content(t)} for t in req.turns]
        tools = [{"name": t.name, "description": t.description, "input_schema": t.schema, "strict": True}
                 for t in req.tools]
        kwargs: dict = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            # Parte estable (prompt + conocimiento + memoria) cacheada; el contexto dinámico va después.
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
            + ([{"type": "text", "text": req.context}] if req.context else []),
            "tools": tools,
            **common_kwargs(req.model, req.effort),
        }
        usage = Usage()
        resp = None
        for _ in range(MAX_TOOL_STEPS):
            resp = await self.client.beta.messages.create(messages=messages, **kwargs)
            add_usage(usage, resp)
            if resp.stop_reason == "refusal":
                category = resp.stop_details.category if resp.stop_details else None
                log.warning("Claude rechazó la respuesta (categoría=%s)", category)
                return AgentResult(text="", refused=True, usage=usage)
            # Se devuelve el contenido completo (thinking, fallback, tool_use) tal cual.
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        output = await execute_tool(block.name, dict(block.input))
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    except Exception as e:  # la herramienta falló: se informa al modelo
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e),
                                        "is_error": True})
            messages.append({"role": "user", "content": results})

        text = "".join(b.text for b in resp.content if b.type == "text") if resp else ""
        return AgentResult(text=text.strip(), usage=usage)
