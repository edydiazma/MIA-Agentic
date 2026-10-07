"""Formato de un journey y su validación.

definition = {
  "start": "s1",
  "steps": [
    {"id": "s1", "type": "send_template", "config": {...}, "next": "w1"},
    {"id": "w1", "type": "wait", "config": {"days": 2}, "next": "b1"},
    {"id": "b1", "type": "branch", "config": {"condition": {"type": "replied", "within_hours": 24}},
     "branches": {"yes": "s2", "no": "ab"}},
    {"id": "ab", "type": "split", "config": {"variants": [{"key": "A", "weight": 50}, {"key": "B", "weight": 50}]},
     "branches": {"A": "s3", "B": "s4"}},
    ...
  ]
}
Cada paso apunta al siguiente con `next` (o termina si no tiene) y las ramas con `branches`. El grafo no puede
tener ciclos (un journey siempre termina) y todos los pasos deben ser alcanzables desde `start`.
"""

import jsonschema

STEP_TYPES = {
    "send_template": "Enviar plantilla de WhatsApp",
    "send_text": "Enviar mensaje (canal automático)",
    "send_email": "Enviar correo",
    "wait": "Esperar",
    "wait_until": "Esperar hasta una hora",
    "branch": "Condición (sí / no)",
    "split": "Prueba A/B",
    "update_contact": "Actualizar cliente",
    "add_tag": "Agregar etiqueta",
    "create_deal": "Crear negocio",
    "move_stage": "Mover etapa",
    "notify_agent": "Avisar a un asesor",
    "start_flow": "Iniciar flujo",
    "exit": "Salir",
}
SEND_TYPES = {"send_template", "send_text", "send_email"}
BRANCH_CONDITIONS = ("replied", "read", "clicked", "field")
ENTRY_TYPES = ("segment_enter", "segment_member", "event", "date_field", "manual", "api")
EVENTS = {
    "conversation_closed": "Conversación cerrada (con tipificación)",
    "deal_won": "Negocio ganado",
    "deal_lost": "Negocio perdido",
    "stage_changed": "Cambio de etapa",
    "appointment_booked": "Cita agendada",
    "appointment_no_show": "No asistió a la cita",
    "order_paid": "Pedido pagado",
    "form_submitted": "Formulario enviado (API)",
}
GOALS = ("replied", "deal_won", "appointment_booked", "purchase", "stage")
MAX_STEPS = 80

_STEP = {
    "type": "object",
    "required": ["id", "type"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"},
        "type": {"enum": list(STEP_TYPES)},
        "config": {"type": "object"},
        "next": {"type": ["string", "null"]},
        "branches": {"type": "object", "additionalProperties": {"type": ["string", "null"]}},
        "position": {"type": "object"},
    },
}
DEFINITION_SCHEMA = {
    "type": "object",
    "required": ["start", "steps"],
    "additionalProperties": False,
    "properties": {
        "start": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "maxItems": MAX_STEPS, "items": _STEP},
    },
}
ENTRY_SCHEMA = {
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {"enum": list(ENTRY_TYPES)},
        "segment_id": {"type": ["integer", "null"]},
        "event": {"enum": [*EVENTS, None]},
        "filter": {"type": "object"},
        "field": {"type": ["string", "null"]},
        "offset_days": {"type": ["integer", "null"], "minimum": -3650, "maximum": 3650},
        "at_time": {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
    },
}
SETTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "reentry": {"enum": ["never", "after_days", "always"]},
        "reentry_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650},
        "frequency_cap": {"type": "object", "properties": {
            "per_day": {"type": ["integer", "null"], "minimum": 1, "maximum": 50},
            "per_week": {"type": ["integer", "null"], "minimum": 1, "maximum": 200}}},
        "quiet_hours": {"type": ["object", "null"], "properties": {
            "from": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
            "to": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
            "timezone": {"type": ["string", "null"]}}},
        "channels_priority": {"type": "array", "items": {"enum": ["whatsapp_cloud", "email", "webchat"]}},
        "require_consent": {"enum": ["marketing", None]},
        "goal": {"type": ["object", "null"], "properties": {
            "event": {"enum": list(GOALS)}, "window_days": {"type": ["integer", "null"], "minimum": 1},
            "pipeline": {"type": ["string", "null"]}, "stage": {"type": ["string", "null"]}}},
    },
}


def _minutes(cfg: dict) -> float:
    return float(cfg.get("minutes") or 0) + float(cfg.get("hours") or 0) * 60 + float(cfg.get("days") or 0) * 1440


def step_errors(step: dict) -> list[str]:
    """Errores de configuración de un paso (lo que el editor muestra en línea)."""
    sid, t, cfg = step["id"], step["type"], step.get("config") or {}
    errs: list[str] = []

    def need(*keys):
        for k in keys:
            if cfg.get(k) in (None, "", []):
                errs.append(f"{sid}: falta «{k}»")

    if t == "send_template":
        need("template_name", "language")
    elif t == "send_text":
        need("text")
        if cfg.get("channel") not in (None, "auto", "whatsapp_cloud", "email", "webchat"):
            errs.append(f"{sid}: canal inválido")
    elif t == "send_email":
        need("subject", "body")
    elif t == "wait":
        if _minutes(cfg) < 1:
            errs.append(f"{sid}: la espera debe ser de al menos 1 minuto")
    elif t == "wait_until":
        import re

        if not re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", str(cfg.get("time") or "")):
            errs.append(f"{sid}: indica la hora como HH:MM")
    elif t == "branch":
        cond = cfg.get("condition") or {}
        if cond.get("type") not in BRANCH_CONDITIONS:
            errs.append(f"{sid}: condición inválida (replied, read, clicked o field)")
        elif cond["type"] == "field":
            if not isinstance(cond.get("rule"), dict):
                errs.append(f"{sid}: falta la regla de la condición")
        elif not 0 < float(cond.get("within_hours") or 0) <= 24 * 30:
            errs.append(f"{sid}: indica en cuántas horas (1 a 720)")
        if set((step.get("branches") or {}).keys()) != {"yes", "no"}:
            errs.append(f"{sid}: la condición necesita las ramas «yes» y «no»")
    elif t == "split":
        variants = cfg.get("variants") or []
        keys = [v.get("key") for v in variants]
        if len(variants) < 2 or len(set(keys)) != len(keys) or not all(keys):
            errs.append(f"{sid}: la prueba A/B necesita al menos 2 variantes con nombre único")
        if sum(int(v.get("weight") or 0) for v in variants) != 100:
            errs.append(f"{sid}: los pesos de las variantes deben sumar 100")
        if set((step.get("branches") or {}).keys()) != set(keys):
            errs.append(f"{sid}: cada variante necesita su rama")
    elif t == "update_contact":
        need("field")
        if "value" not in cfg:
            errs.append(f"{sid}: falta «value»")
    elif t == "add_tag":
        need("tags")
    elif t == "create_deal":
        need("pipeline", "name")
    elif t == "move_stage":
        need("pipeline", "stage")
    elif t == "notify_agent":
        need("title")
        if cfg.get("to", "owner") not in ("owner", "last_agent", "agent"):
            errs.append(f"{sid}: «to» debe ser owner, last_agent o agent")
        if cfg.get("to") == "agent" and not cfg.get("agent_id"):
            errs.append(f"{sid}: falta el asesor")
    elif t == "start_flow":
        need("flow_id")
    if t not in ("branch", "split") and step.get("branches"):
        errs.append(f"{sid}: solo las condiciones y las pruebas A/B tienen ramas")
    return errs


def validate_definition(definition: dict) -> list[str]:
    """Lista de errores (vacía = válido)."""
    try:
        jsonschema.validate(definition, DEFINITION_SCHEMA)
    except jsonschema.ValidationError as e:
        return [f"Formato inválido: {e.message}"]
    steps = definition["steps"]
    by_id: dict[str, dict] = {}
    errs: list[str] = []
    for s in steps:
        if s["id"] in by_id:
            errs.append(f"Paso repetido: {s['id']}")
        by_id[s["id"]] = s
    if definition["start"] not in by_id:
        return errs + ["El paso inicial no existe"]
    for s in steps:
        errs += step_errors(s)
        for target in [s.get("next"), *(s.get("branches") or {}).values()]:
            if target and target not in by_id:
                errs.append(f"{s['id']}: apunta a un paso que no existe ({target})")
    if errs:
        return errs
    # Alcanzabilidad y ciclos (DFS con colores)
    color: dict[str, int] = {}
    cycle: list[str] = []

    def visit(sid: str) -> None:
        if cycle:
            return
        color[sid] = 1
        s = by_id[sid]
        for target in [s.get("next"), *(s.get("branches") or {}).values()]:
            if not target:
                continue
            if color.get(target) == 1:
                cycle.append(target)
                return
            if color.get(target) is None:
                visit(target)
        color[sid] = 2

    visit(definition["start"])
    if cycle:
        errs.append(f"El journey tiene un ciclo (vuelve a {cycle[0]}): un journey siempre debe terminar")
    orphans = [s["id"] for s in steps if s["id"] not in color]
    if orphans:
        errs.append("Pasos que nunca se alcanzan: " + ", ".join(orphans))
    return errs


def validate_entry(entry: dict) -> list[str]:
    try:
        jsonschema.validate(entry, ENTRY_SCHEMA)
    except jsonschema.ValidationError as e:
        return [f"Entrada inválida: {e.message}"]
    t = entry["type"]
    if t in ("segment_enter", "segment_member") and not entry.get("segment_id"):
        return ["Elige el segmento de entrada"]
    if t == "event" and not entry.get("event"):
        return ["Elige el evento de entrada"]
    if t == "date_field":
        if not entry.get("field"):
            return ["Elige el campo de fecha"]
        if not entry.get("at_time"):
            return ["Indica la hora de envío (HH:MM)"]
    return []


def validate_settings(settings: dict) -> list[str]:
    try:
        jsonschema.validate(settings or {}, SETTINGS_SCHEMA)
    except jsonschema.ValidationError as e:
        return [f"Configuración inválida: {e.message}"]
    return []


def ordered_steps(definition: dict) -> list[dict]:
    """Pasos en orden de lectura (BFS desde el inicio) para el embudo y el editor."""
    by_id = {s["id"]: s for s in definition.get("steps") or []}
    out, seen, queue = [], set(), [definition.get("start")]
    while queue:
        sid = queue.pop(0)
        if not sid or sid in seen or sid not in by_id:
            continue
        seen.add(sid)
        s = by_id[sid]
        out.append(s)
        queue += [s.get("next"), *(s.get("branches") or {}).values()]
    return out + [s for s in by_id.values() if s["id"] not in seen]
