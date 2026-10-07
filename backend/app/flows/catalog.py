"""Catálogo de bloques (fuente de verdad para el editor, el validador, el motor y la edición con IA)."""

import json
from pathlib import Path

# Fuente única de verdad del catálogo: blocks.json (el mismo que usa el editor como respaldo,
# frontend/lib/flow-catalog.ts). Si cambias un bloque, cámbialo en ambos y corre tests/test_flows.py.
_DATA = json.loads((Path(__file__).with_name("blocks.json")).read_text(encoding="utf-8"))
CATEGORIES: list[dict] = _DATA["categories"]
BLOCKS: list[dict] = _DATA["blocks"]
TRIGGERS = tuple(b["type"] for b in BLOCKS if b["shape"] == "hat")
BY_TYPE = {b["type"]: b for b in BLOCKS}
assert len(BY_TYPE) == len(BLOCKS), "tipos de bloque duplicados en el catálogo"
OPERATORS = {b["type"] for b in BLOCKS if b["shape"] == "reporter"} | {"neq", "gte", "lte", "empty"}
OTHER_BRANCH = "other"

# Límites del motor
MAX_STEPS = 200
MAX_REPEAT = 20
HTTP_TIMEOUT_S = 10


def catalog() -> dict:
    return {"categories": CATEGORIES, "blocks": BLOCKS, "operators": sorted(OPERATORS)}


def flow_json_schema() -> dict:
    """JSON Schema de flow_versions.definition (lo usa la edición con IA)."""
    block = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": [b["type"] for b in BLOCKS if b["shape"] not in ("hat", "reporter")]},
            "inputs": {"type": "object"},
            "branches": {"type": "object", "description": "then/else, body, o una clave por opción + 'other'"},
            "disabled": {"type": "boolean"},
        },
        "required": ["id", "type", "inputs"],
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "variables": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "type": {"type": "string"}, "default": {}}, "required": ["name"]}},
            "scripts": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "trigger": {"type": "object", "properties": {"type": {"type": "string", "enum": list(TRIGGERS)},
                                                             "config": {"type": "object"}}, "required": ["type"]},
                "blocks": {"type": "array", "items": block},
                "position": {"type": "object"}}, "required": ["id", "trigger", "blocks"]}},
        },
        "required": ["schema_version", "scripts"],
    }
