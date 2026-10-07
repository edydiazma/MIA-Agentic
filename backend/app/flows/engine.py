"""Motor de flujos: ejecuta flow_versions.definition bloque a bloque (ver docs/flows.md).

- Una ejecución (flow_runs) guarda en `context` las variables y una pila de marcos {path, i, repeat_left}
  que apuntan a listas de bloques dentro de la definición; así se puede pausar (wait_reply / wait_time) y
  reanudar exactamente en el mismo punto, incluso dentro de ramas anidadas.
- Cada bloque ejecutado queda en flow_run_steps (entrada, salida, estado, latencia).
- Los cambios se atribuyen al actor 'flow' (conversation_events).
- Modo simulación (dry_run): no escribe en la base ni envía nada; devuelve pasos y mensajes.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, set_actor
from app.flows.catalog import HTTP_TIMEOUT_S, MAX_REPEAT, MAX_STEPS, OTHER_BRANCH
from app.models import (
    Agent,
    ContactField,
    Conversation,
    Flow,
    FlowRun,
    FlowRunStep,
    FlowVersion,
    FollowUp,
    Message,
    Resource,
    utcnow,
)

log = logging.getLogger(__name__)
EXPR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_tasks: set[asyncio.Task] = set()


class FlowError(Exception):
    pass


@dataclass
class Signal:
    kind: str  # push | wait | stop | goto
    path: list | None = None
    repeat_left: int = 0
    resume_at: object = None
    waiting_for: str | None = None
    script_id: str | None = None
    rerun: bool = False  # volver a ejecutar el mismo bloque al reanudar (switch_reply con espera)


@dataclass
class Simulation:
    messages: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    waiting: bool = False


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


# --- Expresiones y condiciones --------------------------------------------------
def _lookup(scope: dict, dotted: str):
    cur = scope
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def render(value, scope: dict):
    """Sustituye {{a.b}}; si el texto es solo una expresión, conserva el tipo (número, lista...)."""
    if isinstance(value, str):
        whole = EXPR.fullmatch(value.strip())
        if whole:
            v = _lookup(scope, whole.group(1))
            return v if v is not None else ""
        return EXPR.sub(lambda m: "" if (v := _lookup(scope, m.group(1))) is None else str(v), value)
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    if isinstance(value, dict):
        if "op" in value:
            return evaluate(value, scope)
        return {k: render(v, scope) for k, v in value.items()}
    return value


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def evaluate(cond, scope: dict):
    if not isinstance(cond, dict) or "op" not in cond:
        return render(cond, scope)
    op = cond["op"]
    # not / length usan "value" (formato del editor); se acepta "left" por compatibilidad
    left = render(cond.get("value", cond.get("left")), scope)
    right = render(cond.get("right"), scope)
    if op == "and":
        return bool(left) and bool(right)
    if op == "or":
        return bool(left) or bool(right)
    if op == "not":
        return not bool(left)
    if op == "empty":
        return left in (None, "", [], {})
    if op == "contains":
        return norm(str(right)) in norm(str(left))
    if op == "join":
        return f"{left}{right}"
    if op == "length":
        return len(left) if hasattr(left, "__len__") else 0
    ln, rn = _num(left), _num(right)
    both = ln is not None and rn is not None
    if op == "eq":
        return ln == rn if both else norm(str(left)) == norm(str(right))
    if op == "neq":
        return ln != rn if both else norm(str(left)) != norm(str(right))
    if not both:
        return False
    return {"gt": ln > rn, "gte": ln >= rn, "lt": ln < rn, "lte": ln <= rn}.get(op, False)


def match_option(reply: str, options: list) -> int | None:
    """Índice de la opción que coincide con la respuesta (por texto o número de opción)."""
    r = norm(reply)
    for i, opt in enumerate(options):
        label = norm(opt if isinstance(opt, str) else opt.get("title") or opt.get("label") or "")
        if label and (r == label or label in r or r == str(i + 1)):
            return i
    return None


# --- Seguridad de http_request --------------------------------------------------
def _safe_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise FlowError("Solo se permiten URLs https://")
    for info in socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FlowError("La URL apunta a una red interna")
    return url


# --- Ejecutor -------------------------------------------------------------------
class Runner:
    def __init__(self, session: AsyncSession | None, definition: dict, run: FlowRun | None,
                 conv: Conversation | None, sim: Simulation | None = None):
        self.session = session
        self.definition = definition
        self.run = run
        self.conv = conv
        self.sim = sim
        self.ctx: dict = dict(run.context) if run else {}

    # -- Utilidades
    def _list(self, path: list) -> list:
        cur = self.definition
        for p in path:
            cur = cur[p]
        return cur

    async def scope(self) -> dict:
        contact, fields_ = {}, {}
        if self.conv:
            c = self.conv.contact
            contact = {"name": c.name or "", "first_name": (c.name or "").split(" ")[0], "wa_id": c.wa_id,
                       "email": c.email or "", "stage": c.stage}
            from app.fields import custom_values

            fields_ = custom_values(c)
        else:
            contact = {"name": "Cliente de prueba", "first_name": "Cliente", "wa_id": "573000000000"}
        conversation = {"id": getattr(self.conv, "id", None), "status": getattr(self.conv, "status", "bot"),
                        "group": getattr(getattr(self.conv, "group", None), "name", None)}
        return {"contact": contact, "fields": fields_, "vars": self.ctx.setdefault("vars", {}),
                "last_message": {"text": self.ctx.get("last_reply", "")}, "conversation": conversation,
                "ai": self.ctx.get("ai", {})}

    async def _log(self, block: dict, status: str, inputs, output, error, started: float) -> None:
        latency = int((time.monotonic() - started) * 1000)
        if self.sim is not None:
            self.sim.steps.append({"block_id": block.get("id"), "type": block.get("type"), "status": status,
                                   "output": output, "error": error})
            return
        self.session.add(FlowRunStep(organization_id=self.run.organization_id, flow_id=self.run.flow_id,
                                     run_id=self.run.id, run_started_at=self.run.started_at, block_id=block["id"],
                                     block_type=block["type"], status=status, input=_jsonable(inputs),
                                     output=_jsonable(output), error=error, latency_ms=latency))

    async def _send_text(self, text: str) -> None:
        if self.sim is not None:
            self.sim.messages.append({"text": text})
            return
        from app.service import send_text

        await send_text(self.session, self.conv, text, sender_type="flow")

    async def _client(self):
        from app.service import wa_client

        return await wa_client(self.session, self.conv.channel)

    async def _record(self, msg: Message) -> None:
        from app.service import record_message

        await record_message(self.session, self.conv, msg)

    # -- Bucle principal
    async def execute(self) -> str:
        """Ejecuta hasta terminar, esperar o detenerse. Devuelve el estado final de la ejecución."""
        frames = self.ctx.setdefault("frames", [])
        while frames:
            frame = frames[-1]
            blocks = self._list(frame["path"])
            if frame["i"] >= len(blocks):
                if frame.get("repeat_left", 0) > 0:
                    frame["repeat_left"] -= 1
                    frame["i"] = 0
                    continue
                frames.pop()
                continue
            index = frame["i"]
            block = blocks[index]
            frame["i"] += 1
            if block.get("disabled"):
                continue
            self.ctx["steps"] = self.ctx.get("steps", 0) + 1
            if self.ctx["steps"] > MAX_STEPS:
                raise FlowError(f"Se superó el máximo de {MAX_STEPS} pasos")

            started = time.monotonic()
            scope = await self.scope()
            inputs = render(block.get("inputs") or {}, scope)
            try:
                signal, output = await self.handle(block, inputs, frame["path"] + [index])
            except FlowError as e:
                await self._log(block, "error", inputs, None, str(e), started)
                raise
            except Exception as e:  # error inesperado de un bloque: queda registrado y la ejecución falla
                await self._log(block, "error", inputs, None, f"{type(e).__name__}: {e}", started)
                raise FlowError(f"Bloque {block.get('id')} ({block.get('type')}): {e}") from e
            await self._log(block, "waiting" if signal and signal.kind == "wait" else "ok", inputs, output, None,
                            started)

            if signal is None:
                continue
            if signal.kind == "push":
                frames.append({"path": signal.path, "i": 0, "repeat_left": signal.repeat_left})
            elif signal.kind == "wait":
                if signal.rerun:
                    frame["i"] -= 1
                self.ctx["waiting_for"] = signal.waiting_for
                self.ctx["wait_block"] = {"id": block["id"], "save_to": (block.get("inputs") or {}).get("save_to")}
                if self.sim is not None:
                    self.sim.waiting = True
                elif self.run:
                    self.run.resume_at = signal.resume_at
                return "waiting"
            elif signal.kind == "stop":
                frames.clear()
            elif signal.kind == "goto":
                idx = next(i for i, s in enumerate(self.definition["scripts"]) if s["id"] == signal.script_id)
                frames[:] = [{"path": ["scripts", idx, "blocks"], "i": 0, "repeat_left": 0}]
        return "succeeded"

    # -- Bloques
    async def handle(self, block: dict, inputs: dict, path: list):  # noqa: C901 (despachador de bloques)
        t = block["type"]
        dry = self.sim is not None
        vars_ = self.ctx.setdefault("vars", {})
        branches = block.get("branches") or {}

        # Mensajes
        if t == "send_text":
            await self._send_text(str(inputs.get("text", "")))
            return None, {"text": inputs.get("text")}
        if t == "send_buttons":
            buttons = [{"id": f"{block['id']}:{i}", "title": str(b if isinstance(b, str) else b.get("title", ""))}
                       for i, b in enumerate(inputs.get("buttons") or [])][:3]
            if dry:
                self.sim.messages.append({"text": inputs.get("text"), "buttons": [b["title"] for b in buttons]})
            else:
                client = await self._client()
                wid = await client.send_buttons(self.conv.contact.wa_id, str(inputs.get("text", "")), buttons)
                await self._record(Message(direction="out", sender_type="flow", type="interactive",
                                           text=str(inputs.get("text", "")), wa_message_id=wid, status="sent",
                                           metadata_={"buttons": [b["title"] for b in buttons]}))
            return None, {"buttons": [b["title"] for b in buttons]}
        if t == "send_list":
            rows = [{"id": f"{block['id']}:{i}", "title": str(o if isinstance(o, str) else o.get("title", ""))[:24]}
                    for i, o in enumerate(inputs.get("items") or inputs.get("options") or [])][:10]
            if dry:
                self.sim.messages.append({"text": inputs.get("text"), "list": [r["title"] for r in rows]})
            else:
                client = await self._client()
                wid = await client.send_list(self.conv.contact.wa_id, str(inputs.get("text", "")),
                                             str(inputs.get("button", "Ver opciones")), [{"title": "Opciones", "rows": rows}])
                await self._record(Message(direction="out", sender_type="flow", type="interactive",
                                           text=str(inputs.get("text", "")), wa_message_id=wid, status="sent",
                                           metadata_={"list": [r["title"] for r in rows]}))
            return None, {"options": [r["title"] for r in rows]}
        if t == "send_media":
            if dry:
                ref = inputs.get("resource_id") or inputs.get("url")
                self.sim.messages.append({"text": f"[archivo {ref}] {inputs.get('caption') or ''}"})
                return None, {"resource": ref}
            if not inputs.get("resource_id"):
                if not str(inputs.get("url", "")).startswith("https://"):
                    raise FlowError("Indica un recurso o una URL https")
                client = await self._client()
                wid = await client.send_image_link(self.conv.contact.wa_id, str(inputs["url"]), inputs.get("caption"))
                await self._record(Message(direction="out", sender_type="flow", type="image", text=inputs.get("caption"),
                                           wa_message_id=wid, status="sent", metadata_={"url": inputs["url"]}))
                return None, {"url": inputs["url"]}
            from app.storage import download, kind_for_mime

            res = await self.session.get(Resource, int(inputs["resource_id"]))
            if not res or res.organization_id != self.conv.organization_id:
                raise FlowError("El recurso no existe")
            data = await download(res.storage_path)
            client = await self._client()
            kind = kind_for_mime(res.mime)
            media_id = await client.upload_media(data, res.mime, res.name)
            wid = await client.send_media(self.conv.contact.wa_id, kind, media_id, inputs.get("caption"), res.name)
            await self._record(Message(direction="out", sender_type="flow", type=kind, text=inputs.get("caption"),
                                       media_path=res.storage_path, media_mime=res.mime, media_filename=res.name,
                                       wa_message_id=wid, status="sent"))
            return None, {"resource": res.name}
        if t == "send_template":
            values = [str(v) for v in inputs.get("values") or []]
            tpl_ref = inputs.get("name") or inputs.get("template") or {}
            name = tpl_ref.get("name") if isinstance(tpl_ref, dict) else str(tpl_ref)
            language = tpl_ref.get("language", "es") if isinstance(tpl_ref, dict) else "es"
            if dry:
                self.sim.messages.append({"text": f"[plantilla {name}] {', '.join(values)}"})
                return None, {"template": name}
            from app import templates
            from app.campaigns import send_template_message

            catalog = await templates.list_templates(self.session, self.conv.channel)
            tpl = next((x for x in catalog if x["name"] == name and x["language"] == language), None)
            if not tpl or not tpl["supported"]:
                raise FlowError(f"Plantilla no disponible: {name}")
            await send_template_message(self.session, self.conv, tpl, values, sender_type="flow")
            return None, {"template": name}
        if t == "send_product":
            if dry:
                self.sim.messages.append({"text": f"[producto {inputs.get('sku') or inputs.get('search')}]"})
                return None, None
            from app import catalog as cat
            from app.models import Product

            product = None
            if inputs.get("sku"):
                product = await self.session.scalar(select(Product).where(
                    Product.organization_id == self.conv.organization_id, Product.sku == str(inputs["sku"])))
            elif inputs.get("search") or inputs.get("query"):
                found = await cat.search(self.session, self.conv.organization_id,
                                         str(inputs.get("search") or inputs.get("query")), limit=1)
                product = found[0] if found else None
            if not product:
                raise FlowError("No se encontró el producto")
            client = await self._client()
            caption = cat.describe(product)[:1000]
            if product.image_url:
                wid = await client.send_image_link(self.conv.contact.wa_id, product.image_url, caption)
                msg_type = "image"
            else:
                wid = (await client.send_text(self.conv.contact.wa_id, caption))[0]
                msg_type = "text"
            await self._record(Message(direction="out", sender_type="flow", type=msg_type, text=caption,
                                       wa_message_id=wid, status="sent", metadata_={"sku": product.sku}))
            return None, {"sku": product.sku}

        # Esperas
        if t == "wait_reply":
            minutes = int(inputs.get("timeout_min") or 60 * 24)
            return Signal("wait", waiting_for="reply", resume_at=utcnow() + timedelta(minutes=minutes)), None
        if t == "wait_time":
            return Signal("wait", waiting_for="time", resume_at=utcnow() + timedelta(minutes=int(inputs["minutes"]))), None

        # Control
        if t == "if":
            ok = bool(evaluate((block.get("inputs") or {}).get("condition"), await self.scope()))
            lane = "then" if ok else "else"
            if branches.get(lane):
                return Signal("push", path=path + ["branches", lane]), {"result": ok}
            return None, {"result": ok}
        if t in ("switch_reply", "ai_decide"):
            options = inputs.get("options") or []
            labels = [str(o if isinstance(o, str) else o.get("title", "")) for o in options]
            if t == "switch_reply" and inputs.get("timeout_min") and self.ctx.get("awaiting_block") != block["id"]:
                # Espera la respuesta y vuelve a este mismo bloque para decidir la rama
                self.ctx["awaiting_block"] = block["id"]
                minutes = int(inputs["timeout_min"])
                return Signal("wait", waiting_for="reply", rerun=True,
                              resume_at=utcnow() + timedelta(minutes=minutes)), None
            self.ctx.pop("awaiting_block", None)
            if t == "switch_reply":
                choice = match_option(self.ctx.get("last_reply", ""), labels)
            else:
                choice = await self._ai_choice(str(inputs.get("question", "")), labels, dry)
            key = labels[choice] if choice is not None else OTHER_BRANCH
            output = {"choice": key}
            if branches.get(key):
                return Signal("push", path=path + ["branches", key]), output
            return None, output
        if t == "repeat":
            times = max(1, min(MAX_REPEAT, int(inputs.get("times") or 1)))
            if branches.get("body"):
                return Signal("push", path=path + ["branches", "body"], repeat_left=times - 1), {"times": times}
            return None, None
        if t == "stop":
            return Signal("stop"), None
        if t == "go_to_script":
            return Signal("goto", script_id=str(inputs["script_id"])), None

        # IA
        if t == "ai_reply":
            if dry:
                self.sim.messages.append({"text": "[La IA respondería aquí con su conocimiento, memoria y catálogo]"})
                return None, None
            from app.agent import run_agent

            await run_agent(self.conv.id)
            return None, {"ai_reply": True}
        if t in ("ai_extract", "ai_classify"):
            target = inputs.get("save_to") or ("ai" if t == "ai_extract" else "categoria")
            if dry:
                vars_[target] = {} if t == "ai_extract" else (inputs.get("options") or [""])[0]
                return None, {"simulated": True}
            data = await self._ai_structured(t, inputs)
            vars_[target] = data
            return None, data
        # Conversación
        if t == "handoff_to_agent":
            if dry:
                self.sim.messages.append({"text": "[Se transfiere a un asesor]"})
                return Signal("stop"), None
            from app.service import handoff

            group_id = int(inputs["group_id"]) if inputs.get("group_id") else None
            await handoff(self.session, self.conv, str(inputs.get("reason") or "Flujo"), group_id, actor="flow")
            return Signal("stop"), {"group_id": group_id}
        if t == "assign":
            if not dry:
                await set_actor(self.session, "flow")
                if inputs.get("group_id"):
                    self.conv.group_id = int(inputs["group_id"])
                if inputs.get("agent_id"):
                    self.conv.assigned_agent_id = int(inputs["agent_id"])
                self.conv.status = "human"
                self.conv.handoff_at = self.conv.handoff_at or utcnow()
                from app.service import commit_and_broadcast

                await commit_and_broadcast(self.session, self.conv)
            return None, inputs
        if t in ("tag", "untag"):
            if not dry:
                from app.service import set_conversation_tags

                current = [link.tag.name for link in self.conv.tag_links]
                name = str(inputs["tag"]).strip().lower()
                names = current + [name] if t == "tag" else [n for n in current if n != name]
                await set_actor(self.session, "flow")
                await set_conversation_tags(self.session, self.conv, names, "flow", replace=True)
                await self.session.commit()
                await self.session.refresh(self.conv, ["tag_links"])
            return None, {"tag": inputs.get("tag")}
        if t == "typify_close":
            if dry:
                self.sim.messages.append({"text": f"[Se cierra como «{inputs.get('typification')}»]"})
                return Signal("stop"), None
            from app.service import close, typification_by_name

            typ = await typification_by_name(self.session, self.conv.organization_id, str(inputs["typification"]))
            if not typ:
                raise FlowError(f"Tipificación inexistente: {inputs['typification']}")
            await close(self.session, self.conv, typ, actor="flow")
            return Signal("stop"), {"typification": typ.name}

        # CRM
        if t == "set_field":
            if not dry:
                from app.fields import coerce, set_custom

                field_ = await self.session.scalar(select(ContactField).where(
                    ContactField.organization_id == self.conv.organization_id, ContactField.key == str(inputs["field"])))
                if not field_:
                    raise FlowError(f"Campo inexistente: {inputs['field']}")
                try:
                    value = coerce(field_, inputs.get("value"))
                except ValueError as e:
                    raise FlowError(str(e)) from e
                await set_custom(self.session, self.conv.contact, field_, value, "flow", conversation_id=self.conv.id)
                await self.session.commit()
            return None, {"field": inputs.get("field"), "value": inputs.get("value")}
        if t in ("set_stage", "update_memory"):
            if not dry:
                from app.fields import set_native

                c = self.conv.contact
                if t == "set_stage":
                    if inputs["stage"] not in ("lead", "prospect", "client", "lost"):
                        raise FlowError("Etapa inválida")
                    set_native(self.session, c, "stage", inputs["stage"], "flow", conversation_id=self.conv.id)
                else:
                    memory = ((c.memory or "") + f"\n- {inputs['text']}").strip()
                    set_native(self.session, c, "memory", memory, "flow", conversation_id=self.conv.id)
                await self.session.commit()
            return None, inputs
        if t == "create_followup":
            if not dry:
                agent_id = int(inputs["agent_id"]) if inputs.get("agent_id") else self.conv.assigned_agent_id
                if not agent_id:
                    agent_id = await self.session.scalar(select(Agent.id).where(
                        Agent.organization_id == self.conv.organization_id, Agent.role == "admin", Agent.is_active)
                        .order_by(Agent.id).limit(1))
                self.session.add(FollowUp(organization_id=self.conv.organization_id, contact_id=self.conv.contact_id,
                                          conversation_id=self.conv.id, agent_id=agent_id, note=str(inputs["note"]),
                                          due_at=utcnow() + timedelta(hours=float(inputs.get("in_hours") or 24))))
                await self.session.commit()
            return None, inputs

        if t == "book_appointment":
            if dry:
                self.sim.messages.append({"text": f"[Cita {inputs.get('date')} {inputs.get('time')}]"})
                return None, inputs
            from app import appointments as appt
            from app.models import Appointment

            cfg, tz = await appt.config(self.session, self.conv.organization_id)
            starts = appt.parse_local(str(inputs["date"]), str(inputs["time"]), tz)
            free = await appt.available_slots(self.session, self.conv.organization_id, starts.date())
            if starts not in free:
                raise FlowError(f"Horario no disponible: {inputs['date']} {inputs['time']}")
            self.session.add(Appointment(organization_id=self.conv.organization_id, contact_id=self.conv.contact_id,
                                         conversation_id=self.conv.id, starts_at=starts, title=cfg["title"],
                                         duration_min=cfg["duration_min"], created_by_type="flow"))
            await self.session.commit()
            return None, inputs

        # Datos
        if t == "set_var":
            vars_[str(inputs["name"])] = inputs.get("value")
            return None, {inputs["name"]: inputs.get("value")}
        if t == "change_var":
            name = str(inputs["name"])
            vars_[name] = (_num(vars_.get(name)) or 0) + (_num(inputs.get("delta")) or 0)
            return None, {name: vars_[name]}
        if t == "http_request":
            if dry:
                return None, {"simulated": True}
            url = _safe_url(str(inputs["url"]))
            body = inputs.get("body")
            if isinstance(body, str) and body.strip():
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    pass
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=False) as http:
                r = await http.request(str(inputs.get("method") or "GET"), url,
                                       json=body if isinstance(body, (dict, list)) else None,
                                       content=body if isinstance(body, str) else None)
            try:
                data = r.json()
            except ValueError:
                data = r.text[:5000]
            if inputs.get("save_to"):
                vars_[str(inputs["save_to"])] = data
            return None, {"status": r.status_code}
        raise FlowError(f"Bloque no soportado: {t}")

    # -- IA a través del Cortex
    async def _ai_structured(self, t: str, inputs: dict) -> dict | str:
        from app.ai.router import json_call

        transcript = await self._transcript()
        if t == "ai_extract":
            keys = [str(k).strip() for k in inputs.get("fields") or [] if str(k).strip()]
            schema = {"type": "object", "properties": {k: {"type": "string"} for k in keys},
                      "required": keys, "additionalProperties": False}
            system = ("Extrae de la conversación los datos pedidos. Si un dato no aparece, devuelve cadena vacía. "
                      "No inventes.")
        else:
            options = [str(o) for o in inputs.get("options") or []]
            schema = {"type": "object", "properties": {"category": {"type": "string", "enum": options}},
                      "required": ["category"], "additionalProperties": False}
            system = "Clasifica la conversación en exactamente una de las categorías."
        data, _ctx = await json_call(self.conv.organization_id, None, "flow", system, transcript, schema,
                                     conversation_id=self.conv.id)
        return data if t == "ai_extract" else data["category"]

    async def _ai_choice(self, question: str, options: list, dry: bool) -> int | None:
        if not options:
            return None
        if dry:
            return 0
        from app.ai.router import json_call

        labels = [str(o if isinstance(o, str) else o.get("title", "")) for o in options]
        schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": labels}},
                  "required": ["choice"], "additionalProperties": False}
        data, _ctx = await json_call(self.conv.organization_id, None, "flow",
                                     f"Responde a la pregunta eligiendo una opción: {question}",
                                     await self._transcript(), schema, conversation_id=self.conv.id)
        return labels.index(data["choice"]) if data.get("choice") in labels else None

    async def _transcript(self) -> str:
        rows = (await self.session.scalars(
            select(Message).where(Message.conversation_id == self.conv.id).order_by(Message.created_at.desc()).limit(40)
        )).all()
        who = {"contact": "Cliente", "bot": "Bot", "agent": "Asesor", "flow": "Bot", "campaign": "Campaña"}
        return "\n".join(f"{who.get(m.sender_type, m.sender_type)}: {m.text or m.transcript or '[' + m.type + ']'}"
                         for m in reversed(rows) if m.sender_type != "system")


def _jsonable(value):
    try:
        json.dumps(value)
        return value if isinstance(value, dict) or value is None else {"value": value}
    except TypeError:
        return {"value": str(value)}


# --- Ciclo de vida de ejecuciones -----------------------------------------------
def script_matches(script: dict, trigger_type: str, text: str | None) -> bool:
    trig = script.get("trigger") or {}
    if trigger_type == "inbound_message":
        config = trig.get("config") or {}
        kws = config.get("keywords") or []
        kws = [k for k in (kws if isinstance(kws, list) else str(kws).split(",")) if norm(str(k))]
        if trig.get("type") == "inbound_message" and not kws:
            return True
        if trig.get("type") in ("keyword", "inbound_message"):
            if config.get("match") == "exact":
                return any(norm(str(k)) == norm(text or "") for k in kws)
            return any(norm(str(k)) in norm(text or "") for k in kws)
        return False
    return trig.get("type") == trigger_type


async def start_run(session: AsyncSession, flow: Flow, version: FlowVersion, script_index: int,
                    conv: Conversation | None, trigger_type: str, text: str | None) -> FlowRun:
    run = FlowRun(organization_id=flow.organization_id, flow_id=flow.id, flow_version_id=version.id,
                  conversation_id=getattr(conv, "id", None), contact_id=getattr(conv, "contact_id", None),
                  trigger_type=trigger_type, status="running",
                  context={"vars": {v["name"]: v.get("default") for v in version.definition.get("variables") or []},
                           "frames": [{"path": ["scripts", script_index, "blocks"], "i": 0, "repeat_left": 0}],
                           "script_id": version.definition["scripts"][script_index]["id"], "last_reply": text or "",
                           "steps": 0})
    session.add(run)
    await session.flush()
    return await advance(session, run, version.definition, conv)


async def advance(session: AsyncSession, run: FlowRun, definition: dict, conv: Conversation | None) -> FlowRun:
    runner = Runner(session, definition, run, conv)
    try:
        status = await runner.execute()
        run.status = status
        if status != "waiting":
            run.resume_at = None
            run.finished_at = utcnow()
    except FlowError as e:
        run.status, run.error, run.finished_at = "failed", str(e)[:2000], utcnow()
        log.warning("Flujo %s falló: %s", run.flow_id, e)
    run.context = dict(runner.ctx)  # reasignar para que se detecte el cambio del JSON
    await session.commit()
    return run


async def _active_flows(session: AsyncSession, org: int, trigger_types: list[str]) -> list[Flow]:
    return list((await session.scalars(
        select(Flow).where(Flow.organization_id == org, Flow.status == "active", Flow.current_version_id.is_not(None))
        .order_by(Flow.priority, Flow.id))).unique().all())


def link_matches(script: dict, link) -> bool:
    """Disparador «Cuando llega por un mensaje disparador»: sin enlaces = cualquiera; si no, por slug o nombre."""
    trig = script.get("trigger") or {}
    if trig.get("type") != "wa_link":
        return False
    wanted = (trig.get("config") or {}).get("links") or []
    wanted = {norm(str(w)) for w in (wanted if isinstance(wanted, list) else str(wanted).split(",")) if norm(str(w))}
    return not wanted or norm(link.slug) in wanted or norm(link.name) in wanted


async def _start_for_link(session: AsyncSession, conv: Conversation, link, text: str) -> bool:
    """Inicia el flujo del mensaje disparador: primero el elegido en el enlace (su script «wa_link» o, si no
    tiene, el primero), luego cualquier flujo activo con un disparador «wa_link» que coincida."""
    flows = await _active_flows(session, conv.organization_id, ["wa_link"])
    chosen = [f for f in flows if f.id == link.flow_id] + [f for f in flows if f.id != link.flow_id]
    for flow in chosen:
        scripts = flow.current_version.definition.get("scripts") or []
        index = next((i for i, sc in enumerate(scripts) if link_matches(sc, link)), None)
        if index is None and flow.id == link.flow_id and scripts:
            index = 0
        if index is not None:
            await set_actor(session, "flow")
            await start_run(session, flow, flow.current_version, index, conv, "wa_link", text)
            return True
    return False


async def handle_inbound(session: AsyncSession, conv: Conversation, msg: Message, link=None) -> bool:
    """Se llama con cada mensaje del cliente. True si un flujo se encargó (el bot de IA no responde).
    `link`: mensaje disparador (wa_links) que trajo este mensaje, si lo hubo (app.attribution.on_inbound)."""
    text = msg.text or msg.transcript or ""
    async with _locks[conv.id]:
        waiting = (await session.scalars(
            select(FlowRun).where(FlowRun.conversation_id == conv.id, FlowRun.status == "waiting")
            .order_by(FlowRun.started_at.desc()).limit(1))).first()
        if waiting and (waiting.context or {}).get("waiting_for") == "reply":
            version = await session.get(FlowVersion, waiting.flow_version_id)
            ctx = dict(waiting.context)
            ctx["last_reply"] = text
            save_to = (ctx.get("wait_block") or {}).get("save_to")
            if save_to:
                ctx.setdefault("vars", {})[save_to] = text
            ctx["waiting_for"] = None
            waiting.context, waiting.status = ctx, "running"
            await set_actor(session, "flow")
            await advance(session, waiting, version.definition, conv)
            return True
        if conv.status != "bot":
            return False
        if link is not None and await _start_for_link(session, conv, link, text):
            return True
        for flow in await _active_flows(session, conv.organization_id, ["inbound_message", "keyword"]):
            version = flow.current_version
            for i, script in enumerate(version.definition.get("scripts") or []):
                if script_matches(script, "inbound_message", text):
                    await set_actor(session, "flow")
                    await start_run(session, flow, version, i, conv, "inbound_message", text)
                    return True
    return False


async def on_event(conversation_id: int, event: str) -> None:
    """Disparadores handoff / close (en segundo plano para no bloquear al llamador)."""
    async def job():
        async with SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if not conv:
                return
            for flow in await _active_flows(session, conv.organization_id, [event]):
                version = flow.current_version
                for i, script in enumerate(version.definition.get("scripts") or []):
                    if script_matches(script, event, None):
                        await set_actor(session, "flow")
                        await start_run(session, flow, version, i, conv, event, None)
    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume_due() -> int:
    """Reanuda ejecuciones cuyo tiempo de espera venció (wait_time, o wait_reply sin respuesta)."""
    resumed = 0
    async with SessionLocal() as session:
        runs = (await session.scalars(select(FlowRun).where(
            FlowRun.status == "waiting", FlowRun.resume_at <= utcnow()).limit(200))).all()
        for run in runs:
            conv = await session.get(Conversation, run.conversation_id) if run.conversation_id else None
            version = await session.get(FlowVersion, run.flow_version_id)
            ctx = dict(run.context)
            if ctx.get("waiting_for") == "reply":
                save_to = (ctx.get("wait_block") or {}).get("save_to")
                if save_to:
                    ctx.setdefault("vars", {})[save_to] = None  # sin respuesta
                ctx["last_reply"] = ""
            ctx["waiting_for"] = None
            run.context, run.status = ctx, "running"
            async with _locks[run.conversation_id or 0]:
                await set_actor(session, "flow")
                await advance(session, run, version.definition, conv)
            resumed += 1
    return resumed


async def resume_loop() -> None:
    while True:
        await asyncio.sleep(20)
        try:
            n = await resume_due()
            if n:
                log.info("Flujos reanudados: %s", n)
        except Exception:
            log.exception("Falló la reanudación de flujos")


async def simulate(definition: dict, text: str) -> Simulation:
    """Ejecuta el primer script que coincide con el texto, sin efectos (para el botón Probar)."""
    sim = Simulation()
    scripts = definition.get("scripts") or []
    idx = next((i for i, s in enumerate(scripts) if script_matches(s, "inbound_message", text)), 0 if scripts else None)
    if idx is None:
        return sim
    runner = Runner(None, definition, None, None, sim)
    runner.ctx = {"vars": {v["name"]: v.get("default") for v in definition.get("variables") or []},
                  "frames": [{"path": ["scripts", idx, "blocks"], "i": 0, "repeat_left": 0}], "last_reply": text}
    try:
        await runner.execute()
    except FlowError as e:
        sim.steps.append({"block_id": None, "type": "error", "status": "error", "output": None, "error": str(e)})
    return sim
