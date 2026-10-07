"""Llamada a un LLM que devuelve JSON validado contra un esquema (una sola conexión; el failover lo hace el Cortex).

- anthropic: structured outputs (output_config.format).
- openai / azure_openai: response_format json_schema estricto.
- openai_compatible: json_schema y, si el endpoint no lo soporta, json_object + validación local.
"""

import json
import logging
from dataclasses import dataclass, field

import anthropic
import openai

from app.ai import anthropic_provider as ap
from app.ai import openai_provider as op
from app.ai.base import Usage

log = logging.getLogger(__name__)
PROVIDERS = ("anthropic", "openai", "openai_compatible", "azure_openai")


class LLMError(Exception):
    """Error del proveedor (red, autenticación, límite...)."""


class LLMRefused(LLMError):
    pass


class LLMInvalid(LLMError):
    """La respuesta llegó pero está fuera de rango (JSON inválido, campos faltantes, reglas del Cortex)."""


@dataclass
class ResolvedConnection:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    params: dict = field(default_factory=dict)  # effort, max_tokens, api_version...


def missing_required(data: dict, schema: dict) -> list[str]:
    return [k for k in schema.get("required", []) if k not in data]


async def complete_json(conn: ResolvedConnection, system: str, user: str, schema: dict,
                        max_tokens: int = 4000) -> tuple[dict, Usage]:
    if conn.provider not in PROVIDERS:
        raise LLMError(f"Proveedor desconocido: {conn.provider}")
    usage = Usage()
    try:
        if conn.provider == "anthropic":
            data = await _anthropic(conn, system, user, schema, max_tokens, usage)
        else:
            data = await _openai(conn, system, user, schema, max_tokens, usage)
    except (anthropic.APIError, openai.APIError) as e:
        raise LLMError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
    if not isinstance(data, dict):
        raise LLMInvalid("El modelo no devolvió un objeto JSON")
    missing = missing_required(data, schema)
    if missing:
        raise LLMInvalid(f"Respuesta incompleta (faltan: {', '.join(missing)})")
    return data, usage


async def _anthropic(conn: ResolvedConnection, system, user, schema, max_tokens, usage: Usage) -> dict:
    kw = ap.common_kwargs(conn.model, conn.params.get("effort"))
    output_config = {**kw.pop("output_config", {}), "format": {"type": "json_schema", "schema": schema}}
    resp = await ap.client(conn.api_key, conn.base_url).beta.messages.create(
        model=conn.model, max_tokens=max_tokens, output_config=output_config,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}], **kw,
    )
    ap.add_usage(usage, resp)
    if resp.stop_reason == "refusal":
        raise LLMRefused("El modelo se negó a responder")
    if resp.stop_reason == "max_tokens":
        raise LLMInvalid("La respuesta se cortó (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMInvalid("JSON inválido") from e


async def _openai(conn: ResolvedConnection, system, user, schema, max_tokens, usage: Usage) -> dict:
    if conn.provider in ("openai_compatible", "azure_openai") and not conn.base_url:
        raise LLMError("Falta la URL base del endpoint")
    client = op.client(conn.provider, conn.api_key, conn.base_url, conn.params)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        resp = await client.chat.completions.create(
            model=conn.model, messages=messages, max_completion_tokens=max_tokens,
            response_format={"type": "json_schema", "json_schema": {"name": "resultado", "schema": schema, "strict": True}},
        )
    except openai.BadRequestError:
        if conn.provider != "openai_compatible":
            raise
        log.info("El endpoint no soporta json_schema; se usa json_object")
        messages[0]["content"] += "\n\nResponde SOLO con un objeto JSON que cumpla este esquema:\n" + json.dumps(schema)
        resp = await client.chat.completions.create(
            model=conn.model, messages=messages, max_tokens=max_tokens, response_format={"type": "json_object"})
    op.add_usage(usage, resp)
    msg = resp.choices[0].message
    if getattr(msg, "refusal", None):
        raise LLMRefused("El modelo se negó a responder")
    try:
        return json.loads(msg.content or "")
    except json.JSONDecodeError as e:
        raise LLMInvalid("JSON inválido") from e
