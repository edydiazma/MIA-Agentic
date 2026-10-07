"""Validación de una definición de flujo (estructura + reglas de negocio). Devuelve errores con ruta."""

from app.flows.catalog import BY_TYPE, OPERATORS, OTHER_BRANCH, TRIGGERS


def _condition_errors(cond, path: str) -> list[dict]:
    if not isinstance(cond, dict) or cond.get("op") not in OPERATORS:
        return [{"path": path, "message": "Condición inválida: usa {op, left, right}"}]
    errors = []
    for side in ("left", "right"):
        if isinstance(cond.get(side), dict) and "op" in cond[side]:
            errors += _condition_errors(cond[side], f"{path}.{side}")
    return errors


def _blocks_errors(blocks, path: str, ids: set[str], script_ids: set[str], depth: int = 0) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(blocks, list):
        return [{"path": path, "message": "Debe ser una lista de bloques"}]
    if depth > 8:
        return [{"path": path, "message": "Demasiados niveles anidados (máx. 8)"}]
    for i, b in enumerate(blocks):
        p = f"{path}[{i}]"
        if not isinstance(b, dict):
            errors.append({"path": p, "message": "Bloque inválido"})
            continue
        bid, btype = b.get("id"), b.get("type")
        if not bid or not isinstance(bid, str):
            errors.append({"path": p, "message": "Falta el id del bloque"})
        elif bid in ids:
            errors.append({"path": p, "message": f"Id repetido: {bid}", "block_id": bid})
        else:
            ids.add(bid)
        spec = BY_TYPE.get(btype)
        if not spec:
            errors.append({"path": p, "message": f"Tipo de bloque desconocido: {btype}", "block_id": bid})
            continue
        if spec["shape"] == "hat":
            errors.append({"path": p, "message": "Un evento solo puede ir al inicio del script", "block_id": bid})
        if spec["shape"] == "reporter":
            errors.append({"path": p, "message": "Un operador solo puede ir dentro de una condición", "block_id": bid})
        inputs = b.get("inputs") or {}
        for inp in spec["inputs"]:
            value = inputs.get(inp["name"])
            if inp["required"] and (value is None or value == "" or value == []):
                errors.append({"path": f"{p}.inputs.{inp['name']}", "message": f"Falta «{inp['label']}»",
                               "block_id": bid})
        if btype == "if" and "condition" in inputs:
            errors += [{**e, "block_id": bid} for e in _condition_errors(inputs["condition"], f"{p}.inputs.condition")]
        if btype == "send_buttons" and len(inputs.get("buttons") or []) > 3:
            errors.append({"path": f"{p}.inputs.buttons", "message": "Máximo 3 botones", "block_id": bid})
        if btype == "send_list" and len(inputs.get("options") or []) > 10:
            errors.append({"path": f"{p}.inputs.options", "message": "Máximo 10 opciones", "block_id": bid})
        if btype == "repeat" and not 1 <= int(inputs.get("times") or 0) <= 20:
            errors.append({"path": f"{p}.inputs.times", "message": "Entre 1 y 20 repeticiones", "block_id": bid})
        if btype == "http_request" and not str(inputs.get("url", "")).startswith("https://"):
            errors.append({"path": f"{p}.inputs.url", "message": "La URL debe ser https://", "block_id": bid})
        if btype == "go_to_script" and inputs.get("script_id") not in script_ids:
            errors.append({"path": f"{p}.inputs.script_id", "message": "El script no existe", "block_id": bid})
        branches = b.get("branches") or {}
        if spec.get("branches") == ["options"]:
            # Una rama por etiqueta de opción + "other"
            allowed = {str(o if isinstance(o, str) else o.get("title", "")) for o in inputs.get("options") or []}
            for name, lane in branches.items():
                if name != OTHER_BRANCH and name not in allowed:
                    errors.append({"path": f"{p}.branches.{name}", "message": f"Rama sin opción: {name}",
                                   "block_id": bid})
                    continue
                errors += _blocks_errors(lane, f"{p}.branches.{name}", ids, script_ids, depth + 1)
        else:
            for name, lane in branches.items():
                if name not in (spec.get("branches") or []):
                    errors.append({"path": f"{p}.branches.{name}", "message": f"Rama no permitida: {name}",
                                   "block_id": bid})
                    continue
                errors += _blocks_errors(lane, f"{p}.branches.{name}", ids, script_ids, depth + 1)
    return errors


def validate_definition(definition) -> list[dict]:
    if not isinstance(definition, dict):
        return [{"path": "$", "message": "La definición debe ser un objeto JSON"}]
    errors: list[dict] = []
    if definition.get("schema_version") != 1:
        errors.append({"path": "schema_version", "message": "schema_version debe ser 1"})
    scripts = definition.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        return errors + [{"path": "scripts", "message": "El flujo necesita al menos un script"}]
    script_ids = {s.get("id") for s in scripts if isinstance(s, dict)}
    if len(script_ids) != len(scripts):
        errors.append({"path": "scripts", "message": "Los scripts necesitan ids únicos"})
    names = [v.get("name") for v in definition.get("variables") or [] if isinstance(v, dict)]
    if len(names) != len(set(names)):
        errors.append({"path": "variables", "message": "Variables repetidas"})
    ids: set[str] = set()
    for i, s in enumerate(scripts):
        p = f"scripts[{i}]"
        trig = (s or {}).get("trigger") or {}
        if trig.get("type") not in TRIGGERS:
            errors.append({"path": f"{p}.trigger", "message": f"Disparador inválido: {trig.get('type')}"})
        elif trig["type"] == "keyword" and not (trig.get("config") or {}).get("keywords"):
            errors.append({"path": f"{p}.trigger.config.keywords", "message": "Indica las palabras clave"})
        errors += _blocks_errors(s.get("blocks"), f"{p}.blocks", ids, script_ids)
    return errors
