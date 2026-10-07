"""Webhooks entrantes (docs/data-model.md §17): un sistema externo (agenda de taller, DMS, ERP) llama a
/hooks/{slug} con parámetros; se validan por tipo, se mapean (destinatario, variables de la plantilla, campos del
cliente, llaves maestras, vehículo, negocio, cita, variables del flujo) y se ejecuta la acción.
"""

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_actor
from app.models import (
    AIAgent,
    Appointment,
    Channel,
    Contact,
    ContactVehicle,
    Conversation,
    Deal,
    Flow,
    FlowVersion,
    InboundWebhook,
    InboundWebhookRun,
    Message,
    Organization,
    PipelineStage,
    Typification,
    utcnow,
)

log = logging.getLogger(__name__)
ACTIONS = ("send_template", "send_text", "start_flow", "upsert_contact", "create_deal", "create_appointment")
PARAM_TYPES = ("text", "number", "date", "datetime", "phone", "email", "url", "currency")
MAPS_RE = re.compile(
    r"^(recipient\.(phone|bsuid)|template\.(header\.1|body\.[\w]+|button\.\d+)|message\.text|contact\.(name|email)"
    r"|field:[a-z][a-z0-9_]*|key:[a-z][a-z0-9_]*|vehicle\.[a-z_]+|deal\.[a-z_]+|appointment\.[a-z_]+"
    r"|flow\.var\.[A-Za-z_][\w]*)$")
SENSITIVE_MAPS = ("key:document", "key:birthdate", "key:address")
COUNTRY_CODES = {"CO": "57", "MX": "52", "PE": "51", "CL": "56", "AR": "54", "EC": "593", "VE": "58", "PA": "507",
                 "CR": "506", "GT": "502", "DO": "1", "US": "1", "ES": "34", "BR": "55", "UY": "598", "PY": "595",
                 "BO": "591", "HN": "504", "SV": "503", "NI": "505"}
NATIONAL_LEN = {"57": 10, "52": 10, "51": 9, "56": 9, "54": 10, "593": 9, "58": 10, "507": 8, "506": 8, "502": 8,
                "1": 10, "34": 9, "55": 11, "598": 8, "595": 9, "591": 8, "504": 8, "503": 8, "505": 8}


class HookError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or []


# --- Tokens y slugs ---------------------------------------------------------------------------------------
def new_token() -> str:
    return "whk_" + secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def slugify(name: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "webhook"


async def unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    for _ in range(10):
        slug = f"{base}-{secrets.token_hex(3)}"
        if not await session.scalar(select(InboundWebhook.id).where(InboundWebhook.slug == slug)):
            return slug
    raise HookError(503, "slug", "No se pudo generar la URL")


# --- Validación de la configuración -------------------------------------------------------------------------
def validate_config(action: str, params: list[dict], options: dict) -> list[dict]:
    """Normaliza y valida los parámetros. Lanza ValueError con un mensaje en español."""
    if action not in ACTIONS:
        raise ValueError(f"Acción inválida: {action}")
    out, names = [], set()
    for raw in params or []:
        name = str(raw.get("name") or "").strip()
        if not re.match(r"^[A-Za-z_][\w]{0,59}$", name):
            raise ValueError(f"Nombre de parámetro inválido: «{name}» (letras, números y _)")
        if name in names:
            raise ValueError(f"Parámetro repetido: {name}")
        names.add(name)
        ptype = raw.get("type") or "text"
        if ptype not in PARAM_TYPES:
            raise ValueError(f"Tipo inválido en {name}: {ptype}")
        maps_to = (raw.get("maps_to") or "").strip() or None
        if maps_to and not MAPS_RE.match(maps_to):
            raise ValueError(f"Destino inválido en {name}: {maps_to}")
        out.append({"name": name, "label": raw.get("label") or name, "type": ptype,
                    "required": bool(raw.get("required", False)), "example": raw.get("example"), "maps_to": maps_to})
    targets = [p["maps_to"] for p in out if p["maps_to"]]
    if action != "upsert_contact" and not any(t in ("recipient.phone", "recipient.bsuid") for t in targets):
        raise ValueError("Mapea un parámetro al destinatario (recipient.phone o recipient.bsuid)")
    if action == "send_text" and "message.text" not in targets:
        raise ValueError("La acción «enviar texto» necesita un parámetro mapeado a message.text")
    if options.get("bot") not in (None, "on", "off", "keep"):
        raise ValueError("options.bot debe ser on, off o keep")
    return out


# --- Valores ----------------------------------------------------------------------------------------------
def coerce(param: dict, raw) -> object:
    ptype = param["type"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    value = str(raw).strip() if not isinstance(raw, (int, float)) else raw
    if ptype in ("number", "currency"):
        if not isinstance(value, str):
            return float(value)
        try:
            return parse_number(value)
        except ValueError as e:
            raise ValueError("debe ser un número") from e
    if ptype == "date":
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(str(value), fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError("debe ser una fecha (AAAA-MM-DD o DD/MM/AAAA)")
    if ptype == "datetime":
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
        except ValueError as e:
            raise ValueError("debe ser fecha y hora ISO 8601") from e
    if ptype == "phone":
        digits = re.sub(r"\D", "", str(value))
        if not 7 <= len(digits) <= 15:
            raise ValueError("debe ser un teléfono")
        return digits
    if ptype == "email":
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(value)):
            raise ValueError("debe ser un correo")
        return str(value).lower()
    if ptype == "url":
        if not re.match(r"^https?://", str(value)):
            raise ValueError("debe ser una URL http(s)")
    return str(value)[:2000]


def parse_number(value: str) -> float:
    """Números con separadores de miles latinos o anglos: 45.000 → 45000; 1.234,5 → 1234.5; 1,234.5 → 1234.5."""
    v = re.sub(r"[^\d.,\-]", "", value.strip())
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", v):
        v = v.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", v):
        v = v.replace(",", "")
    else:
        v = v.replace(",", ".")
    return float(v)


def normalize_phone(digits: str | None, country: str | None) -> str | None:
    """Agrega el indicativo del país de la empresa a un número nacional (ej. 3101234567 → 573101234567)."""
    if not digits:
        return None
    code = COUNTRY_CODES.get((country or "CO").upper(), "57")
    if len(digits) == NATIONAL_LEN.get(code, 10) and not digits.startswith(code):
        return code + digits
    return digits


def display_value(param: dict, value) -> str:
    if param.get("type") == "date" and isinstance(value, str):
        try:
            return date.fromisoformat(value).strftime("%d/%m/%Y")
        except ValueError:
            return value
    if param.get("type") == "currency" and isinstance(value, float):
        return f"{value:,.0f}".replace(",", ".")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def mask(param: dict, value) -> object:
    if value is None or not (param.get("maps_to") or "").startswith(SENSITIVE_MAPS):
        return value
    s = str(value)
    return "*" * max(len(s) - 4, 2) + s[-4:] if len(s) > 4 else "****"


@dataclass
class Parsed:
    values: dict = field(default_factory=dict)  # nombre → valor tipado
    by_target: dict = field(default_factory=dict)  # maps_to → valor


def parse_params(hook: InboundWebhook, payload: dict) -> Parsed:
    errors, parsed = [], Parsed()
    for p in hook.params or []:
        raw = payload.get(p["name"])
        try:
            value = coerce(p, raw)
        except ValueError as e:
            errors.append({"param": p["name"], "message": str(e)})
            continue
        if value is None:
            if p.get("required"):
                errors.append({"param": p["name"], "message": "es obligatorio"})
            continue
        parsed.values[p["name"]] = value
        if p.get("maps_to"):
            parsed.by_target[p["maps_to"]] = (p, value)
    if errors:
        raise HookError(422, "invalid_params", "Parámetros inválidos", errors)
    return parsed


def dedupe_key(hook: InboundWebhook, parsed: Parsed) -> str:
    body = json.dumps(parsed.values, sort_keys=True, default=str)
    return "auto:" + hashlib.sha256(f"{hook.id}:{hook.version}:{body}".encode()).hexdigest()[:40]


# --- Ejecución ---------------------------------------------------------------------------------------------
async def _channel(session: AsyncSession, hook: InboundWebhook) -> Channel:
    channel = await session.get(Channel, hook.channel_id) if hook.channel_id else (await session.scalars(
        select(Channel).where(Channel.organization_id == hook.organization_id,
                              Channel.provider == "whatsapp_cloud").order_by(Channel.id).limit(1))).first()
    if channel is None or channel.organization_id != hook.organization_id:
        raise HookError(409, "no_channel", "El webhook no tiene un número de WhatsApp configurado")
    return channel


async def _contact(session: AsyncSession, hook: InboundWebhook, parsed: Parsed) -> Contact | None:
    from app.service import get_or_create_contact

    org = await session.get(Organization, hook.organization_id)
    phone = parsed.by_target.get("recipient.phone")
    bsuid = parsed.by_target.get("recipient.bsuid")
    phone_value = normalize_phone(phone[1] if phone else None, org.country)
    if not phone_value and not bsuid:
        return None
    name = parsed.by_target.get("contact.name")
    contact, _new = await get_or_create_contact(session, hook.organization_id, phone_value,
                                                name[1] if name else None, bsuid=bsuid[1] if bsuid else None)
    return contact


async def _apply_contact_maps(session: AsyncSession, contact: Contact, conv: Conversation | None,
                              parsed: Parsed) -> dict:
    from app.fields import coerce as coerce_field
    from app.fields import fields_by_key, set_custom

    done: dict = {}
    fields = None
    for target, (_p, value) in parsed.by_target.items():
        if target == "contact.name" and value and not contact.name:
            contact.name = str(value)
        elif target == "contact.email" and value:
            contact.email = str(value)
        elif target.startswith("field:"):
            fields = fields if fields is not None else await fields_by_key(session, contact.organization_id)
            f = fields.get(target.split(":", 1)[1])
            if f is not None:
                try:
                    await set_custom(session, contact, f, coerce_field(f, value), "api", None,
                                     conv.id if conv else None)
                    done.setdefault("fields", []).append(f.key)
                except ValueError:
                    log.debug("Valor inválido para el campo %s", f.key)
        elif target.startswith("key:"):
            if await _golden_key(session, contact, target.split(":", 1)[1], value, conv):
                done.setdefault("keys", []).append(target.split(":", 1)[1])
    vehicle = {t.split(".", 1)[1]: v for t, (_p, v) in parsed.by_target.items() if t.startswith("vehicle.")}
    if vehicle.get("plate") or vehicle.get("vin"):
        done["vehicle_id"] = await _vehicle(session, contact, vehicle, conv)
    return done


async def _golden_key(session: AsyncSession, contact: Contact, key_type: str, value, conv) -> bool:
    """Registra una llave maestra si el módulo de registro maestro está disponible."""
    try:
        from app.golden import keys as golden_keys
    except ImportError:
        return False
    upsert = getattr(golden_keys, "upsert_key", None)
    if upsert is None:
        return False
    try:
        await upsert(session, contact, key_type, str(value), source="api", confidence=1.0,
                     conversation_id=conv.id if conv else None)
        return True
    except TypeError:
        try:
            await upsert(session, contact, key_type, str(value), source="api")
            return True
        except Exception:  # noqa: BLE001
            log.exception("No se pudo registrar la llave %s", key_type)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo registrar la llave %s", key_type)
    return False


async def _vehicle(session: AsyncSession, contact: Contact, attrs: dict, conv) -> int:
    plate = re.sub(r"[^A-Z0-9]", "", str(attrs.get("plate") or "").upper()) or None
    vin = re.sub(r"[^A-Z0-9]", "", str(attrs.get("vin") or "").upper()) or None
    q = select(ContactVehicle).where(ContactVehicle.contact_id == contact.id)
    q = q.where(ContactVehicle.plate == plate) if plate else q.where(ContactVehicle.vin == vin)
    v = (await session.scalars(q.limit(1))).first()
    if v is None:
        v = ContactVehicle(organization_id=contact.organization_id, contact_id=contact.id, plate=plate, vin=vin,
                           source="api", conversation_id=conv.id if conv else None)
        session.add(v)
    columns = {"make", "model", "version", "color", "fuel"}
    extra = {}
    for k, val in attrs.items():
        if k in columns:
            setattr(v, k, str(val))
        elif k == "year":
            v.year = int(float(val))
        elif k == "mileage_km":
            v.mileage_km = int(float(val))
        elif k in ("insurance_due", "inspection_due", "warranty_until", "next_service_at"):
            setattr(v, k, date.fromisoformat(str(val)[:10]))
        elif k not in ("plate", "vin"):
            extra[k] = val
    if vin and not v.vin:
        v.vin = vin
    if extra:
        v.attributes = {**(v.attributes or {}), **extra}
    await session.flush()
    return v.id


async def _template_send(session: AsyncSession, conv: Conversation, hook: InboundWebhook, parsed: Parsed,
                         channel: Channel) -> Message:
    from app import templates
    from app.identity import require_phone_for_template, wa_address
    from app.service import record_message, wa_client

    catalog = await templates.list_templates(session, channel)
    tpl = next((t for t in catalog if t["name"] == hook.template_name
                and t["language"] == (hook.template_language or t["language"])), None)
    if tpl is None or tpl["status"] != "APPROVED":
        raise HookError(409, "template_unavailable", f"La plantilla «{hook.template_name}» no existe o no está aprobada")
    values = []
    for i, var in enumerate(tpl["variables"], start=1):
        hit = parsed.by_target.get(f"template.body.{var}") or parsed.by_target.get(f"template.body.{i}")
        if hit is None:
            raise HookError(422, "missing_template_var", f"Falta la variable {{{{{var}}}}} de la plantilla")
        values.append(display_value(hit[0], hit[1]))
    components, rendered = templates.build(tpl, values)
    header = parsed.by_target.get("template.header.1")
    if header:
        components.insert(0, {"type": "header", "parameters": [{"type": "text", "text": display_value(*header)}]})
    for target, (p, value) in sorted(parsed.by_target.items()):
        if target.startswith("template.button."):
            components.append({"type": "button", "sub_type": "url", "index": str(int(target.rsplit(".", 1)[1]) - 1),
                               "parameters": [{"type": "text", "text": display_value(p, value)}]})
    # Optimización de costos: dentro de la ventana de atención el mismo texto va como mensaje libre (sin costo)
    if (hook.options or {}).get("prefer_free_form") and await _cost_optimized(session, conv) and \
            await _window_open(session, conv):
        from app.service import send_text

        return (await send_text(session, conv, rendered, sender_type="bot"))[0]
    msg = Message(direction="out", sender_type="bot", type="template", text=rendered, template_name=tpl["name"])
    try:
        require_phone_for_template(conv.contact, tpl.get("category"))
        msg.wa_message_id = await (await wa_client(session, channel)).send_template(
            wa_address(conv.contact), tpl["name"], tpl["language"], components)
        msg.status = "sent"
    except Exception as e:  # noqa: BLE001 — se registra como fallido y la ejecución también
        msg.status, msg.error = "failed", str(e)[:2000]
    msg.metadata_ = {"inbound_webhook_id": hook.id}
    return await record_message(session, conv, msg)


async def _cost_optimized(session: AsyncSession, conv: Conversation) -> bool:
    agent_id = conv.ai_agent_id or conv.channel.default_ai_agent_id
    agent = await session.get(AIAgent, agent_id) if agent_id else None
    return bool(agent and agent.cost_optimization)


async def _window_open(session: AsyncSession, conv: Conversation) -> bool:
    from app.recovery import window_end

    end = await window_end(session, conv)
    return end is not None and utcnow() < end


async def _start_flow(session: AsyncSession, conv: Conversation, hook: InboundWebhook, parsed: Parsed) -> int:
    from app.flows.engine import start_run

    flow = await session.get(Flow, hook.flow_id) if hook.flow_id else None
    if flow is None or flow.organization_id != hook.organization_id or flow.status != "active" \
            or not flow.current_version_id:
        raise HookError(409, "flow_unavailable", "El flujo del webhook no existe o no está activo")
    version = await session.get(FlowVersion, flow.current_version_id)
    scripts = version.definition.get("scripts") or []
    index = next((i for i, sc in enumerate(scripts) if (sc.get("trigger") or {}).get("type") == "webhook"), 0)
    variables = {t.split(".", 2)[2]: v for t, (_p, v) in parsed.by_target.items() if t.startswith("flow.var.")}
    variables["webhook"] = parsed.values
    await set_actor(session, "flow")
    run = await start_run(session, flow, version, index, conv, "webhook", None, initial_vars=variables)
    return run.id


async def _create_deal(session: AsyncSession, conv: Conversation, contact: Contact, hook: InboundWebhook,
                       parsed: Parsed) -> int:
    from app.agent_config import set_stage

    attrs = {t.split(".", 1)[1]: v for t, (_p, v) in parsed.by_target.items() if t.startswith("deal.")}
    pipeline = str(attrs.pop("pipeline", None) or (hook.options or {}).get("pipeline") or "default")
    stage_key = attrs.pop("stage", None)
    if stage_key and await session.scalar(select(PipelineStage.id).where(
            PipelineStage.organization_id == hook.organization_id, PipelineStage.pipeline == pipeline,
            PipelineStage.key == str(stage_key))):
        deal = await set_stage(session, conv, pipeline, str(stage_key), f"Webhook «{hook.name}»", source="webhook")
    else:
        from app.crm import pipeline as pl

        config = await pl.get_pipelines(session, hook.organization_id)
        keys = pl.stage_keys(config, pipeline) or ["new"]
        deal = Deal(organization_id=hook.organization_id, contact_id=contact.id, conversation_id=conv.id,
                    name=f"{hook.name} · {contact.name or 'Cliente'}", pipeline=pipeline,
                    stage=str(stage_key) if stage_key in keys else keys[0], source="api")
        session.add(deal)
    if attrs.get("name"):
        deal.name = str(attrs.pop("name"))
    if attrs.get("amount") is not None:
        deal.amount = float(attrs.pop("amount"))
    if attrs.get("currency"):
        deal.currency = str(attrs.pop("currency")).upper()[:3]
    if attrs:
        deal.attributes = {**(deal.attributes or {}), **{k: v for k, v in attrs.items()}}
    await session.flush()
    return deal.id


async def _create_appointment(session: AsyncSession, conv: Conversation, contact: Contact, hook: InboundWebhook,
                              parsed: Parsed) -> int:
    from zoneinfo import ZoneInfo

    from app import appointments

    attrs = {t.split(".", 1)[1]: v for t, (_p, v) in parsed.by_target.items() if t.startswith("appointment.")}
    cfg, tz = await appointments.config(session, hook.organization_id)
    starts = None
    if attrs.get("starts_at"):
        starts = datetime.fromisoformat(str(attrs["starts_at"]))
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=tz if isinstance(tz, ZoneInfo) else UTC)
    elif attrs.get("date") and attrs.get("time"):
        starts = appointments.parse_local(str(attrs["date"]), str(attrs["time"])[:5], tz)
    if starts is None:
        raise HookError(422, "invalid_params", "La cita necesita appointment.starts_at o appointment.date + time")
    appt = Appointment(organization_id=hook.organization_id, contact_id=contact.id, conversation_id=conv.id,
                       starts_at=starts.astimezone(UTC),
                       duration_min=int(float(attrs.get("duration_min") or cfg["duration_min"])),
                       title=str(attrs.get("title") or cfg["title"]), notes=str(attrs.get("notes") or "") or None,
                       created_by_type="webhook")
    session.add(appt)
    await session.flush()
    return appt.id


async def _apply_options(session: AsyncSession, conv: Conversation, hook: InboundWebhook) -> None:
    from app.service import set_conversation_tags

    opts = hook.options or {}
    if opts.get("assign_group_id"):
        conv.group_id = int(opts["assign_group_id"])
    if opts.get("assign_agent_id"):
        conv.assigned_agent_id = int(opts["assign_agent_id"])
        conv.status = "human" if conv.status != "closed" else conv.status
    if opts.get("bot") == "off" and conv.status == "bot":
        conv.status, conv.handoff_reason, conv.handoff_at = "human", f"Webhook «{hook.name}»", utcnow()
    elif opts.get("bot") == "on" and conv.status == "human" and not opts.get("assign_agent_id"):
        conv.status, conv.assigned_agent_id = "bot", None
    if opts.get("tags"):
        await session.refresh(conv, ["tag_links"])
        await set_conversation_tags(session, conv, list(opts["tags"]), "rule", replace=False)
    if opts.get("typification"):
        typ = await session.scalar(select(Typification).where(
            Typification.organization_id == hook.organization_id, Typification.name == opts["typification"]))
        if typ:
            conv.typification_id = typ.id


async def execute(session: AsyncSession, hook: InboundWebhook, payload: dict, dry_run: bool = False) -> dict:
    """Ejecuta (o simula) el webhook. Devuelve el resultado; lanza HookError con el código HTTP."""
    from app.agent_config import log_event
    from app.service import get_or_create_conversation, send_text

    parsed = parse_params(hook, payload)
    if dry_run:
        preview = {"values": parsed.values, "targets": {t: v for t, (_p, v) in parsed.by_target.items()}}
        if hook.action == "send_template" and hook.template_name:
            preview["template"] = {"name": hook.template_name, "language": hook.template_language}
        return {"ok": True, "dry_run": True, **preview}
    await set_actor(session, "api")
    channel = await _channel(session, hook) if hook.action != "upsert_contact" or hook.channel_id else None
    contact = await _contact(session, hook, parsed)
    if contact is None:
        raise HookError(422, "missing_recipient", "Falta el destinatario (teléfono o BSUID)")
    conv = await get_or_create_conversation(session, channel, contact, reopen=False) if channel else None
    result: dict = {"contact_id": contact.id, "conversation_id": conv.id if conv else None}
    result.update(await _apply_contact_maps(session, contact, conv, parsed))
    if hook.action == "send_template":
        msg = await _template_send(session, conv, hook, parsed, channel)
        result["message_id"] = msg.id
        if msg.status == "failed":
            raise HookError(502, "send_failed", f"WhatsApp rechazó el mensaje: {msg.error}", [result])
    elif hook.action == "send_text":
        if not await _window_open(session, conv):
            raise HookError(409, "window_closed", "Fuera de la ventana de 24 h: usa una plantilla")
        msg = (await send_text(session, conv, display_value(*parsed.by_target["message.text"]), sender_type="bot"))[0]
        result["message_id"] = msg.id
        if msg.status == "failed":
            raise HookError(502, "send_failed", f"No se pudo enviar: {msg.error}", [result])
    elif hook.action == "start_flow":
        result["flow_run_id"] = await _start_flow(session, conv, hook, parsed)
    elif hook.action == "create_deal":
        result["deal_id"] = await _create_deal(session, conv, contact, hook, parsed)
    elif hook.action == "create_appointment":
        result["appointment_id"] = await _create_appointment(session, conv, contact, hook, parsed)
    if conv is not None:
        await session.refresh(conv)
        await _apply_options(session, conv, hook)
        await set_actor(session, "api")
        await log_event(session, conv.id, "webhook_triggered", {"webhook_id": hook.id, "name": hook.name,
                                                                "action": hook.action})
    await session.commit()
    if conv is not None:
        from app.service import commit_and_broadcast

        await commit_and_broadcast(session, conv)
    return result


async def find_duplicate(session: AsyncSession, hook: InboundWebhook, key: str, window: timedelta) -> int | None:
    return await session.scalar(select(InboundWebhookRun.id).where(
        InboundWebhookRun.webhook_id == hook.id, InboundWebhookRun.idempotency_key == key,
        InboundWebhookRun.status == "succeeded", InboundWebhookRun.created_at >= utcnow() - window)
        .order_by(InboundWebhookRun.created_at.desc()).limit(1))


async def record_run(session: AsyncSession, hook: InboundWebhook, status: str, http_status: int, payload: dict | None,
                     result: dict | None, error: str | None, started: float, ip: str | None,
                     idem: str | None) -> int:
    import time

    masked = None
    if payload is not None:
        by_name = {p["name"]: p for p in hook.params or []}
        masked = {k: mask(by_name.get(k, {}), v) for k, v in list(payload.items())[:60]}
        raw = json.dumps(masked, default=str)
        if len(raw) > 16000:
            masked = {"_truncated": raw[:16000]}
    run = InboundWebhookRun(organization_id=hook.organization_id, webhook_id=hook.id, status=status,
                            http_status=http_status, payload=masked, result=result,
                            contact_id=(result or {}).get("contact_id"),
                            conversation_id=(result or {}).get("conversation_id"), error=(error or "")[:2000] or None,
                            latency_ms=int((time.monotonic() - started) * 1000), ip=ip, idempotency_key=idem)
    session.add(run)
    await session.commit()
    return run.id


async def rate_limited(session: AsyncSession, hook: InboundWebhook, per_min: int = 600) -> bool:
    hits = await session.scalar(text("select public.rate_limit_hit(:b, 60)"), {"b": f"hook:{hook.id}"})
    await session.commit()
    return bool(hits and hits > per_min)


async def params_from_template(session: AsyncSession, channel: Channel, name: str, language: str | None) -> list[dict]:
    """Parámetros sugeridos desde una plantilla: destinatario + una variable por {{n}} del cuerpo."""
    from app import templates

    catalog = await templates.list_templates(session, channel)
    tpl = next((t for t in catalog if t["name"] == name and (not language or t["language"] == language)), None)
    if tpl is None:
        raise HookError(404, "template_not_found", "Plantilla no encontrada")
    params = [{"name": "telefono", "label": "Teléfono del cliente", "type": "phone", "required": True,
               "example": "3001234567", "maps_to": "recipient.phone"},
              {"name": "nombre", "label": "Nombre del cliente", "type": "text", "required": False,
               "example": "Ana", "maps_to": "contact.name"}]
    for i, var in enumerate(tpl["variables"], start=1):
        pname = var if not var.isdigit() else f"var_{var}"
        params.append({"name": pname, "label": f"Variable {{{{{var}}}}}", "type": "text", "required": True,
                       "example": None, "maps_to": f"template.body.{var if tpl['named'] else i}"})
    return params


def params_count(hook: InboundWebhook) -> int:
    return len(hook.params or [])


async def counts_by_status(session: AsyncSession, hook_id: int) -> dict:
    rows = (await session.execute(select(InboundWebhookRun.status, func.count()).where(
        InboundWebhookRun.webhook_id == hook_id).group_by(InboundWebhookRun.status))).all()
    return dict(rows)
