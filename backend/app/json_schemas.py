"""JSON Schemas de las entidades editables como documento ("estilo n8n"): por personas, importación o IA.

Los documentos nunca contienen secretos (las claves viven en Vault).
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.agent_config import RecoveryAttempt, SourceRule
from app.settings_store import DEFAULTS

AUTOMATION_TYPES = ("welcome", "keyword_reply", "keyword_handoff", "business_hours", "inactivity_close")
ENTITY_TYPES = ("setting", "classifier", "ai_agent", "cortex", "automation", "flow")


class AIAgentDoc(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    enabled: bool = True
    cortex_id: int | None = None
    system_prompt: str = Field(min_length=1)
    handoff_message: str = Field(min_length=1)
    use_knowledge: bool = True
    use_memory: bool = True
    use_customer_memory: bool = True
    use_catalog: bool = True
    use_appointments: bool = True
    # Configuración avanzada (§17): reglas por fuente, recuperación por inactividad, seguridad
    timezone: str | None = None
    max_words: int | None = Field(default=None, ge=20, le=2000)
    ad_context_enabled: bool = True
    ad_context_prompt: str | None = None
    source_rules: list[SourceRule] = []
    cost_optimization: bool = True
    security_enabled: bool = True
    security_prompt: str | None = None
    security_action: Literal["close", "block", "handoff", "flag"] = "close"
    recovery_enabled: bool = False
    recovery_attempts: list[RecoveryAttempt] = Field(default=[], max_length=3)
    inactivity_end_hours: float | None = Field(default=None, gt=0, le=72)
    inactivity_end_typification_id: int | None = None
    extract_field_ids: list[int] = []


class CortexMemberDoc(BaseModel):
    connection_id: int
    position: int | None = None
    weight: int = Field(default=1, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1000, le=600000)


class CortexValidationDoc(BaseModel):
    min_chars: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=1)
    banned_phrases: list[str] = []


class CortexDoc(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    purpose: Literal["chat", "classification", "learning", "flow", "json_edit", "qa", "agent_test", "onboarding", "golden", "any"] = "any"
    strategy: Literal["failover", "lowest_latency", "weighted"] = "failover"
    max_latency_ms: int | None = Field(default=None, ge=100)
    max_attempts: int = Field(default=3, ge=1, le=10)
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_s: int = Field(default=120, ge=1)
    validation: CortexValidationDoc = CortexValidationDoc()
    is_active: bool = True
    members: list[CortexMemberDoc] = []


class AutomationDoc(BaseModel):
    name: str = Field(min_length=1)
    type: Literal["welcome", "keyword_reply", "keyword_handoff", "business_hours", "inactivity_close"]
    config: dict = {}
    enabled: bool = True
    priority: int = 100


FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "variables": {"type": "object"},
        "scripts": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "trigger": {"type": "object"},
                           "blocks": {"type": "array", "items": {"type": "object", "required": ["type"]}}},
        }},
    },
    "required": ["scripts"],
}


def _json_type(value) -> dict:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "number"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {"type": "object"}
    return {}  # None: cualquier tipo


def setting_schema(key: str) -> dict:
    props = {k: _json_type(v) for k, v in DEFAULTS[key].items()}
    if key == "classifier":
        props["cortex_id"] = {"type": ["integer", "null"]}
        props["min_confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
        props["tags"] = {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string", "minLength": 1},
                                             "description": {"type": "string"}}, "required": ["name"]}}
        props["typification_criteria"] = {"type": "object", "additionalProperties": {"type": "string"}}
        props["modes"] = {"type": "object", "properties": {
            "tags": {"enum": ["auto", "suggest", "off"]}, "group": {"enum": ["auto", "suggest", "off"]},
            "typification": {"enum": ["auto", "suggest", "off"]},
            "fields": {"enum": ["fill_empty", "overwrite_ai", "suggest", "off"]},
            "memory": {"enum": ["auto", "off"]}}}
    if key == "learning":
        props["cortex_id"] = {"type": ["integer", "null"]}
    if key == "catalog":
        props["last_sync"] = {}
        props["feed_format"] = {"enum": ["csv", "json"]}
    if key == "appointments":
        props["days"] = {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}}
    return {"type": "object", "properties": props, "additionalProperties": False}


def schema_for(entity_type: str, entity_id: str | int | None = None) -> dict:
    if entity_type in ("setting", "classifier"):
        key = "classifier" if entity_type == "classifier" else str(entity_id)
        return setting_schema(key)
    return {
        "ai_agent": AIAgentDoc.model_json_schema(),
        "cortex": CortexDoc.model_json_schema(),
        "automation": AutomationDoc.model_json_schema(),
        "flow": FLOW_SCHEMA,
    }[entity_type]
