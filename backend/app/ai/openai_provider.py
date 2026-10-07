import base64
import json

import openai

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


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _message(turn: Turn) -> dict:
    if turn.role == "assistant":
        return {"role": "assistant", "content": "\n".join(p.text for p in turn.parts if isinstance(p, TextPart))}
    content: list[dict] = []
    for p in turn.parts:
        if isinstance(p, TextPart):
            content.append({"type": "text", "text": p.text})
        elif isinstance(p, ImagePart):
            content.append({"type": "image_url", "image_url": {"url": _data_url(p.data, p.mime)}})
        elif isinstance(p, DocumentPart):
            content.append({"type": "file", "file": {"filename": p.filename, "file_data": _data_url(p.data, p.mime)}})
    return {"role": "user", "content": content}


def client(provider: str, api_key: str | None, base_url: str | None, params: dict | None = None):
    """openai | openai_compatible (cualquier endpoint compatible) | azure_openai."""
    params = params or {}
    if provider == "azure_openai":
        return openai.AsyncAzureOpenAI(api_key=api_key, azure_endpoint=base_url,
                                       api_version=params.get("api_version", "2024-10-21"), max_retries=0)
    return openai.AsyncOpenAI(api_key=api_key or "sin-clave", base_url=base_url or None, max_retries=0)


def add_usage(usage: Usage, resp) -> None:
    u = getattr(resp, "usage", None)
    if u:
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        usage.add(u.prompt_tokens, u.completion_tokens, cached)


class OpenAIProvider:
    def __init__(self, provider: str = "openai", api_key: str | None = None, base_url: str | None = None,
                 params: dict | None = None):
        self.client = client(provider, api_key, base_url, params)

    async def run(self, req: AgentRequest, execute_tool: ToolExecutor) -> AgentResult:
        messages: list[dict] = [{"role": "system", "content": f"{req.system}\n\n{req.context}".strip()}]
        messages += [_message(t) for t in req.turns]
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
                 for t in req.tools]
        usage = Usage()
        msg = None
        for _ in range(MAX_TOOL_STEPS):
            resp = await self.client.chat.completions.create(
                model=req.model, messages=messages, tools=tools or openai.omit, max_completion_tokens=req.max_tokens)
            add_usage(usage, resp)
            msg = resp.choices[0].message
            if msg.refusal:
                return AgentResult(text="", refused=True, usage=usage)
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                break
            for call in msg.tool_calls:
                try:
                    output = await execute_tool(call.function.name, json.loads(call.function.arguments or "{}"))
                except Exception as e:
                    output = f"ERROR: {e}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        return AgentResult(text=(msg.content or "").strip() if msg else "", usage=usage)
