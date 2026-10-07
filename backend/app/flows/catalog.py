"""Catálogo de bloques (fuente de verdad para el editor, el validador, el motor y la edición con IA)."""

CATEGORIES = [
    {"key": "events", "label": "Eventos", "color": "#FFBF00"},
    {"key": "messages", "label": "Mensajes", "color": "#4C97FF"},
    {"key": "wait", "label": "Esperar", "color": "#59C059"},
    {"key": "control", "label": "Control", "color": "#FFAB19"},
    {"key": "ai", "label": "IA (Cortex)", "color": "#9966FF"},
    {"key": "conversation", "label": "Conversación", "color": "#CF63CF"},
    {"key": "crm", "label": "CRM", "color": "#0FBD8C"},
    {"key": "data", "label": "Datos", "color": "#FF8C1A"},
    {"key": "operators", "label": "Operadores", "color": "#59C059"},
]

TRIGGERS = ("inbound_message", "keyword", "handoff", "close", "schedule", "webhook", "manual", "campaign_reply")


def _i(name, label, type_="text", required=False, options=None):
    d = {"name": name, "label": label, "type": type_, "required": required}
    if options:
        d["options"] = options
    return d


BLOCKS: list[dict] = [
    # Eventos (hat): un único bloque por script, define el disparador
    {"type": "inbound_message", "category": "events", "label": "Cuando llega un mensaje", "icon": "💬",
     "junior": True, "shape": "hat", "inputs": []},
    {"type": "keyword", "category": "events", "label": "Cuando el mensaje contiene", "icon": "🔑", "junior": True,
     "shape": "hat", "inputs": [_i("keywords", "Palabras (separadas por coma)", "text", True)]},
    {"type": "handoff", "category": "events", "label": "Cuando se transfiere a un asesor", "icon": "🙋",
     "junior": True, "shape": "hat", "inputs": []},
    {"type": "close", "category": "events", "label": "Cuando se cierra la conversación", "icon": "✅",
     "junior": True, "shape": "hat", "inputs": []},
    {"type": "schedule", "category": "events", "label": "Cada cierto tiempo", "icon": "⏰", "junior": False,
     "shape": "hat", "inputs": [_i("every_minutes", "Cada (minutos)", "number", True)]},
    {"type": "webhook", "category": "events", "label": "Cuando llega un webhook", "icon": "🔗", "junior": False,
     "shape": "hat", "inputs": []},
    {"type": "manual", "category": "events", "label": "Cuando un asesor lo ejecuta", "icon": "▶️", "junior": True,
     "shape": "hat", "inputs": []},
    {"type": "campaign_reply", "category": "events", "label": "Cuando responden una campaña", "icon": "📣",
     "junior": True, "shape": "hat", "inputs": [_i("campaign_id", "Campaña (opcional)", "number")]},
    # Mensajes
    {"type": "send_text", "category": "messages", "label": "Enviar texto", "icon": "✉️", "junior": True,
     "shape": "stack", "inputs": [_i("text", "Texto", "text", True)]},
    {"type": "send_media", "category": "messages", "label": "Enviar archivo", "icon": "📎", "junior": True,
     "shape": "stack", "inputs": [_i("resource_id", "Recurso", "resource", True), _i("caption", "Texto")]},
    {"type": "send_buttons", "category": "messages", "label": "Enviar botones", "icon": "🔘", "junior": True,
     "shape": "stack", "inputs": [_i("text", "Texto", "text", True), _i("buttons", "Botones (máx. 3)", "buttons", True)]},
    {"type": "send_list", "category": "messages", "label": "Enviar lista", "icon": "📋", "junior": False,
     "shape": "stack", "inputs": [_i("text", "Texto", "text", True), _i("button", "Botón", "text", True),
                                  _i("options", "Opciones (máx. 10)", "options", True)]},
    {"type": "send_template", "category": "messages", "label": "Enviar plantilla", "icon": "📄", "junior": True,
     "shape": "stack", "inputs": [_i("template", "Plantilla", "template", True), _i("values", "Variables", "options")]},
    {"type": "send_product", "category": "messages", "label": "Enviar producto", "icon": "🛍️", "junior": True,
     "shape": "stack", "inputs": [_i("sku", "SKU"), _i("query", "o buscar")]},
    # Esperar
    {"type": "wait_reply", "category": "wait", "label": "Esperar respuesta", "icon": "⏳", "junior": True,
     "shape": "stack", "inputs": [_i("timeout_min", "Máximo (min)", "number"), _i("save_to", "Guardar en variable")]},
    {"type": "wait_time", "category": "wait", "label": "Esperar", "icon": "🕒", "junior": True, "shape": "stack",
     "inputs": [_i("minutes", "Minutos", "number", True)]},
    # Control
    {"type": "if", "category": "control", "label": "Si", "icon": "❓", "junior": True, "shape": "c",
     "inputs": [_i("condition", "Condición", "condition", True)], "branches": ["then", "else"]},
    {"type": "switch_reply", "category": "control", "label": "Si responde…", "icon": "🔀", "junior": True,
     "shape": "c", "inputs": [_i("options", "Opciones", "options", True), _i("timeout_min", "Esperar (minutos)", "duration")],
     "branches": ["options"]},
    {"type": "repeat", "category": "control", "label": "Repetir", "icon": "🔁", "junior": False, "shape": "c",
     "inputs": [_i("times", "Veces (máx. 20)", "number", True)], "branches": ["body"]},
    {"type": "stop", "category": "control", "label": "Detener", "icon": "⛔", "junior": True, "shape": "cap",
     "inputs": []},
    {"type": "go_to_script", "category": "control", "label": "Ir a", "icon": "↪️", "junior": False, "shape": "cap",
     "inputs": [_i("script_id", "Script", "text", True)]},
    # IA
    {"type": "ai_reply", "category": "ai", "label": "Que responda la IA", "icon": "🤖", "junior": True,
     "shape": "stack", "inputs": [_i("ai_agent_id", "Agente", "ai_agent"), _i("instruction", "Instrucción extra")]},
    {"type": "ai_extract", "category": "ai", "label": "Extraer datos con IA", "icon": "🧲", "junior": False,
     "shape": "stack", "inputs": [_i("fields", "Datos a extraer", "options", True), _i("save_to", "Guardar en variable")]},
    {"type": "ai_classify", "category": "ai", "label": "Clasificar con IA", "icon": "🏷️", "junior": False,
     "shape": "stack", "inputs": [_i("options", "Categorías", "options", True), _i("save_to", "Guardar en variable", "text", True)]},
    {"type": "ai_decide", "category": "ai", "label": "Que decida la IA", "icon": "🧠", "junior": False, "shape": "c",
     "inputs": [_i("question", "Pregunta", "text", True), _i("options", "Opciones", "options", True)],
     "branches": ["options"]},
    # Conversación
    # "handoff" es el evento; la acción se llama handoff_to_agent
    {"type": "handoff_to_agent", "category": "conversation", "label": "Pasar a asesor", "icon": "🙋", "junior": True,
     "shape": "stack", "inputs": [_i("group_id", "Grupo", "group"), _i("reason", "Motivo")]},
    {"type": "assign", "category": "conversation", "label": "Asignar", "icon": "👤", "junior": False,
     "shape": "stack", "inputs": [_i("agent_id", "Asesor", "agent"), _i("group_id", "Grupo", "group")]},
    {"type": "tag", "category": "conversation", "label": "Etiquetar", "icon": "🏷️", "junior": True,
     "shape": "stack", "inputs": [_i("tag", "Etiqueta", "tag", True)]},
    {"type": "untag", "category": "conversation", "label": "Quitar etiqueta", "icon": "🧽", "junior": False,
     "shape": "stack", "inputs": [_i("tag", "Etiqueta", "tag", True)]},
    {"type": "typify_close", "category": "conversation", "label": "Cerrar con tipificación", "icon": "✅",
     "junior": True, "shape": "cap", "inputs": [_i("typification", "Tipificación", "typification", True)]},
    # CRM
    {"type": "set_field", "category": "crm", "label": "Guardar dato del cliente", "icon": "📝", "junior": True,
     "shape": "stack", "inputs": [_i("field", "Campo", "field", True), _i("value", "Valor", "text", True)]},
    {"type": "set_stage", "category": "crm", "label": "Cambiar etapa", "icon": "📈", "junior": True,
     "shape": "stack", "inputs": [_i("stage", "Etapa", "select", True, ["lead", "prospect", "client", "lost"])]},
    {"type": "update_memory", "category": "crm", "label": "Anotar en la memoria", "icon": "🧠", "junior": False,
     "shape": "stack", "inputs": [_i("text", "Texto", "text", True)]},
    {"type": "create_followup", "category": "crm", "label": "Crear seguimiento", "icon": "📌", "junior": True,
     "shape": "stack", "inputs": [_i("agent_id", "Asesor", "agent"), _i("in_hours", "En (horas)", "number", True),
                                  _i("note", "Nota", "text", True)]},
    {"type": "book_appointment", "category": "crm", "label": "Agendar cita", "icon": "📅", "junior": False,
     "shape": "stack", "inputs": [_i("date", "Fecha (AAAA-MM-DD)", "text", True), _i("time", "Hora (HH:MM)", "text", True)]},
    # Datos
    {"type": "set_var", "category": "data", "label": "Fijar variable", "icon": "🔤", "junior": False,
     "shape": "stack", "inputs": [_i("name", "Variable", "text", True), _i("value", "Valor", "text", True)]},
    {"type": "change_var", "category": "data", "label": "Sumar a variable", "icon": "➕", "junior": False,
     "shape": "stack", "inputs": [_i("name", "Variable", "text", True), _i("delta", "Cantidad", "number", True)]},
    {"type": "http_request", "category": "data", "label": "Llamar una API", "icon": "🌐", "junior": False,
     "shape": "stack", "inputs": [_i("method", "Método", "select", True, ["GET", "POST", "PUT", "PATCH", "DELETE"]),
                                  _i("url", "URL (https)", "text", True), _i("body", "Cuerpo JSON"),
                                  _i("save_to", "Guardar en variable")]},
]
# Operadores (reporteros): solo dentro de entradas de tipo condición, como {"op", "left", "right"}
for _op, _label, _icon in [("eq", "=", "="), ("gt", ">", ">"), ("lt", "<", "<"), ("contains", "contiene", "∋"),
                           ("and", "y", "∧"), ("or", "o", "∨"), ("not", "no", "¬"), ("join", "unir", "⧺"),
                           ("length", "longitud de", "#")]:
    BLOCKS.append({"type": _op, "category": "operators", "label": _label, "icon": _icon, "junior": False,
                   "shape": "reporter",
                   "inputs": [_i("left", "Izquierda")] + ([] if _op in ("not", "length") else [_i("right", "Derecha")])})
BY_TYPE = {b["type"]: b for b in BLOCKS}
assert len(BY_TYPE) == len(BLOCKS), "tipos de bloque duplicados en el catálogo"
OTHER_BRANCH = "other"
OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "and", "or", "not", "empty", "join", "length"}

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
