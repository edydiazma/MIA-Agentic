"""Motor de journeys (docs/data-model.md §21.1).

Cada inscripción avanza por el grafo de su versión. `run_enrollment` ejecuta pasos hasta encontrar una espera (o
terminar); una espera deja `next_run_at` y el programador vuelve a encolarla. Garantías:
- **Una sola vez por paso de envío**: antes de enviar se marca el paso en `context.sent` y se hace commit; un
  reintento (worker caído, timeout) ve la marca y no reenvía (a lo sumo una vez, nunca dos).
- **Límites**: frecuencia por cliente (journeys + campañas, por día / semana), horas de silencio (corren el envío),
  consentimiento de marketing, opt-out y bloqueo (salida del journey).
- **Canal**: WhatsApp libre dentro de las 24 h; fuera, la plantilla de respaldo; correo si el cliente lo tiene;
  chat web solo si el visitante está conectado.
- **A/B estable**: la variante sale del hash de la inscripción y el paso (siempre la misma para ese cliente).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, set_actor
from app.models import (
    Agent,
    Channel,
    Contact,
    ContactIdentity,
    ContactTag,
    Conversation,
    Deal,
    DealStageEvent,
    Journey,
    JourneyEnrollment,
    JourneyEvent,
    JourneyVersion,
    Message,
    Organization,
    utcnow,
)

log = logging.getLogger(__name__)
MAX_STEPS_PER_RUN = 40
WEBCHAT_ACTIVE_S = 600
LINK_RE = re.compile(r"\{\{\s*link:(https?://[^\s}]+)\s*\}\}")
ACTIVE = ("active", "waiting")


# --- Utilidades -----------------------------------------------------------------------------------------------
async def org_tz(session: AsyncSession, org: int, override: str | None = None) -> ZoneInfo:
    if override:
        try:
            return ZoneInfo(override)
        except Exception:  # noqa: BLE001 — zona inválida: la de la empresa
            pass
    o = await session.get(Organization, org)
    return ZoneInfo(o.timezone or "America/Bogota")


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, quiet: dict | None, tz: ZoneInfo) -> datetime | None:
    """Si `now` cae en las horas de silencio, devuelve cuándo terminan (UTC); si no, None."""
    if not quiet or not quiet.get("from") or not quiet.get("to"):
        return None
    local = now.astimezone(tz)
    start, end = _hm(quiet["from"]), _hm(quiet["to"])
    t = local.time()
    if start <= end:
        inside = start <= t < end
        end_day = local.date()
    else:  # cruza la medianoche (20:00 → 08:00)
        inside = t >= start or t < end
        end_day = local.date() + timedelta(days=1) if t >= start else local.date()
    if not inside:
        return None
    return datetime.combine(end_day, end, tz).astimezone(UTC)


def next_time_of_day(now: datetime, hhmm: str, tz: ZoneInfo, weekdays: list[int] | None = None) -> datetime:
    """Próxima ocurrencia de HH:MM (hora local), opcionalmente solo ciertos días (0 = lunes)."""
    local = now.astimezone(tz)
    target = _hm(hhmm)
    for add in range(0, 8):
        day = local.date() + timedelta(days=add)
        cand = datetime.combine(day, target, tz)
        if cand > local and (not weekdays or cand.weekday() in weekdays):
            return cand.astimezone(UTC)
    return (local + timedelta(days=1)).astimezone(UTC)


def variant_for(enrollment_id: int, step_id: str, variants: list[dict]) -> str:
    bucket = int(hashlib.sha256(f"{enrollment_id}:{step_id}".encode()).hexdigest(), 16) % 100
    acc = 0
    for v in variants:
        acc += int(v.get("weight") or 0)
        if bucket < acc:
            return v["key"]
    return variants[-1]["key"]


def _sign(payload: str) -> str:
    return hmac.new(get_settings().jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]


def click_token(enrollment: JourneyEnrollment, step_id: str, url: str) -> str:
    raw = f"{enrollment.id}|{int(enrollment.enrolled_at.timestamp() * 1_000_000)}|{step_id}|{url}"
    return f"{quote(raw, safe='')}.{_sign(raw)}"


def parse_click_token(token: str) -> tuple[int, datetime, str, str] | None:
    from urllib.parse import unquote

    raw_q, _, sig = token.rpartition(".")
    raw = unquote(raw_q)
    if not raw or not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        eid, us, step, url = raw.split("|", 3)
        return int(eid), datetime.fromtimestamp(int(us) / 1_000_000, UTC), step, url
    except ValueError:
        return None


def tracked_text(body: str, enrollment: JourneyEnrollment, step_id: str) -> str:
    """{{link:https://…}} → enlace con seguimiento de clics (/j/c?t=…)."""
    base = get_settings().public_base_url.rstrip("/")
    return LINK_RE.sub(lambda m: f"{base}/j/c?t={click_token(enrollment, step_id, m.group(1))}", body)


def render(body: str, contact: Contact) -> str:
    first = (contact.name or "").split(" ")[0]
    return (body.replace("{{nombre}}", first or "").replace("{{first_name}}", first or "")
            .replace("{{name}}", contact.name or "").replace("{{nombre_completo}}", contact.name or ""))


async def add_event(session: AsyncSession, e: JourneyEnrollment, step_id: str, kind: str,
                    message_id: int | None = None, data: dict | None = None) -> None:
    session.add(JourneyEvent(organization_id=e.organization_id, journey_id=e.journey_id, enrollment_id=e.id,
                             contact_id=e.contact_id, step_id=step_id, variant=e.variant, kind=kind,
                             message_id=message_id, data=data or {}))


async def get_enrollment(session: AsyncSession, enrollment_id: int, enrolled_at: datetime | None = None,
                         lock: bool = False) -> JourneyEnrollment | None:
    q = select(JourneyEnrollment).where(JourneyEnrollment.id == enrollment_id)
    if enrolled_at is not None:
        q = q.where(JourneyEnrollment.enrolled_at == enrolled_at)
    if lock:
        q = q.with_for_update(skip_locked=True)
    return (await session.scalars(q.limit(1))).first()


# --- Inscripción ----------------------------------------------------------------------------------------------
async def enroll(session: AsyncSession, journey: Journey, contact_id: int, source: str,
                 data: dict | None = None, schedule: bool = True) -> JourneyEnrollment | None:
    """Inscribe a un cliente (None si no corresponde: journey inactivo, ya activo, reingreso no permitido,
    bloqueado). Hace commit y programa la primera ejecución."""
    if journey.status != "active" or not journey.current_version_id:
        return None
    contact = await session.get(Contact, contact_id)
    if not contact or contact.organization_id != journey.organization_id or contact.blocked:
        return None
    previous = (await session.scalars(select(JourneyEnrollment).where(
        JourneyEnrollment.journey_id == journey.id, JourneyEnrollment.contact_id == contact_id)
        .order_by(JourneyEnrollment.enrolled_at.desc()).limit(1))).first()
    if previous is not None:
        if previous.status in ACTIVE:
            return None
        policy = (journey.settings or {}).get("reentry", "never")
        if policy == "never":
            return None
        if policy == "after_days":
            days = int((journey.settings or {}).get("reentry_days") or 30)
            last = previous.finished_at or previous.enrolled_at
            if last and utcnow() - last < timedelta(days=days):
                return None
    version = await session.get(JourneyVersion, journey.current_version_id)
    e = JourneyEnrollment(organization_id=journey.organization_id, journey_id=journey.id, version_id=version.id,
                          contact_id=contact_id, status="active", current_step=version.definition["start"],
                          next_run_at=utcnow(), context={"source": source, **({"entry": data} if data else {})})
    session.add(e)
    await session.flush()
    await add_event(session, e, version.definition["start"], "entered", data={"source": source})
    await session.commit()
    if schedule:
        await schedule_run(session, e)
    return e


async def schedule_run(session: AsyncSession, e: JourneyEnrollment) -> None:
    """Encola la ejecución en la cola «outbound» (o la deja para el programador si la cola está apagada)."""
    from app import jobs

    if not jobs.enabled():
        return
    await jobs.enqueue(session, "journeys.run", {"enrollment_id": e.id, "enrolled_at": e.enrolled_at.isoformat()},
                       queue="outbound", organization_id=e.organization_id,
                       dedupe_key=f"jr:{e.id}:{int((e.next_run_at or utcnow()).timestamp())}")
    await session.commit()


async def finish(session: AsyncSession, e: JourneyEnrollment, status: str, reason: str | None = None) -> None:
    e.status, e.finished_at, e.next_run_at = status, utcnow(), None
    if reason:
        e.exit_reason = reason
    kind = {"completed": "completed", "goal_met": "goal_met"}.get(status, "exited")
    await add_event(session, e, e.current_step or "-", kind, data={"reason": reason} if reason else None)


# --- Ejecución ----------------------------------------------------------------------------------------------
class Ctx:
    def __init__(self, session: AsyncSession, e: JourneyEnrollment, journey: Journey, definition: dict,
                 contact: Contact, now: datetime):
        self.session, self.e, self.journey, self.contact, self.now = session, e, journey, contact, now
        self.steps = {s["id"]: s for s in definition["steps"]}
        self.settings = journey.settings or {}
        self.context = dict(e.context or {})

    def save(self) -> None:
        self.e.context = dict(self.context)  # reasignar para que se detecte el cambio del JSON

    def mark(self, bucket: str, step_id: str, value=True) -> None:
        self.context.setdefault(bucket, {})[step_id] = value
        self.save()

    def marked(self, bucket: str, step_id: str):
        return (self.context.get(bucket) or {}).get(step_id)


async def run_enrollment(enrollment_id: int, enrolled_at: datetime | None = None,
                         now: datetime | None = None) -> str | None:
    """Ejecuta una inscripción hasta la próxima espera o el final. Devuelve el estado resultante."""
    async with SessionLocal() as session:
        e = await get_enrollment(session, enrollment_id, enrolled_at, lock=True)
        if e is None or e.status not in ACTIVE:
            return getattr(e, "status", None)
        now = now or utcnow()
        if e.next_run_at and e.next_run_at > now + timedelta(seconds=1):
            return e.status
        journey = await session.get(Journey, e.journey_id)
        version = await session.get(JourneyVersion, e.version_id)
        contact = await session.get(Contact, e.contact_id)
        if journey is None or version is None or contact is None:
            return None
        if journey.status == "paused":  # en pausa: se queda donde está
            e.next_run_at = now + timedelta(minutes=15)
            await session.commit()
            return e.status
        if journey.status == "archived":
            await finish(session, e, "exited", "journey_archived")
            await session.commit()
            return e.status
        await set_actor(session, "automation")
        ctx = Ctx(session, e, journey, version.definition, contact, now)
        for _ in range(MAX_STEPS_PER_RUN):
            if contact.blocked:
                await finish(session, e, "exited", "blocked")
                break
            step = ctx.steps.get(e.current_step or "")
            if step is None:
                await finish(session, e, "completed")
                break
            try:
                outcome, value = await execute_step(ctx, step)
            except Exception as ex:  # noqa: BLE001 — el paso falla, la inscripción termina con el error
                log.exception("Journey %s: el paso %s falló", journey.id, step["id"])
                await session.rollback()
                e = await get_enrollment(session, enrollment_id, enrolled_at)
                await add_event(session, e, step["id"], "failed", data={"error": str(ex)[:500]})
                await finish(session, e, "failed", f"{step['type']}: {str(ex)[:200]}")
                await session.commit()
                return e.status
            if outcome == "wait":
                e.status, e.next_run_at = "waiting", value
                break
            if outcome == "exit":
                await finish(session, e, "exited", value)
                break
            e.current_step, e.status, e.next_run_at = value, "active", now
            if value is None:
                await finish(session, e, "completed")
                break
        await session.commit()
        if e.status == "active":  # quedan pasos (tope por ejecución): siguiente vuelta
            await schedule_run(session, e)
        return e.status


async def execute_step(ctx: Ctx, step: dict) -> tuple[str, object]:
    t, cfg, sid = step["type"], step.get("config") or {}, step["id"]
    nxt = step.get("next")
    if t in ("send_template", "send_text", "send_email"):
        return await _send_step(ctx, step)
    if t == "wait":
        until = ctx.marked("wait", sid)
        if until is None:
            minutes = (float(cfg.get("minutes") or 0) + float(cfg.get("hours") or 0) * 60
                       + float(cfg.get("days") or 0) * 1440)
            until = (ctx.now + timedelta(minutes=max(1.0, minutes))).isoformat()
            ctx.mark("wait", sid, until)
            await add_event(ctx.session, ctx.e, sid, "waited", data={"until": until})
        due = datetime.fromisoformat(until)
        return ("next", nxt) if ctx.now >= due else ("wait", due)
    if t == "wait_until":
        until = ctx.marked("wait", sid)
        if until is None:
            tz = await org_tz(ctx.session, ctx.e.organization_id, (ctx.settings.get("quiet_hours") or {}).get("timezone"))
            until = next_time_of_day(ctx.now, cfg["time"], tz, cfg.get("weekdays")).isoformat()
            ctx.mark("wait", sid, until)
            await add_event(ctx.session, ctx.e, sid, "waited", data={"until": until})
        due = datetime.fromisoformat(until)
        return ("next", nxt) if ctx.now >= due else ("wait", due)
    if t == "branch":
        return await _branch(ctx, step)
    if t == "split":
        variants = cfg.get("variants") or []
        chosen = ctx.marked("variant", sid) or variant_for(ctx.e.id, sid, variants)
        if not ctx.marked("variant", sid):
            ctx.mark("variant", sid, chosen)
            if ctx.e.variant is None:
                ctx.e.variant = chosen
            await add_event(ctx.session, ctx.e, sid, "branched", data={"variant": chosen})
        return "next", (step.get("branches") or {}).get(chosen)
    if t == "update_contact":
        await _update_contact(ctx, cfg)
        return "next", nxt
    if t == "add_tag":
        from app.service import tag_by_name

        for name in cfg.get("tags") or []:
            tag = await tag_by_name(ctx.session, ctx.e.organization_id, str(name))
            if tag and not await ctx.session.get(ContactTag, (ctx.contact.id, tag.id)):
                ctx.session.add(ContactTag(contact_id=ctx.contact.id, tag_id=tag.id, source="rule"))
        return "next", nxt
    if t == "create_deal":
        exists = await ctx.session.scalar(select(Deal.id).where(
            Deal.contact_id == ctx.contact.id, Deal.pipeline == cfg["pipeline"], Deal.status == "open").limit(1))
        if not exists:
            ctx.session.add(Deal(organization_id=ctx.e.organization_id, contact_id=ctx.contact.id,
                                 name=render(str(cfg["name"]), ctx.contact), pipeline=cfg["pipeline"],
                                 stage=cfg.get("stage") or "new", amount=cfg.get("amount"), source="flow",
                                 owner_agent_id=ctx.contact.owner_agent_id, origin=f"journey:{ctx.journey.id}"))
        return "next", nxt
    if t == "move_stage":
        deal = (await ctx.session.scalars(select(Deal).where(
            Deal.contact_id == ctx.contact.id, Deal.pipeline == cfg["pipeline"], Deal.status == "open")
            .order_by(Deal.id.desc()).limit(1))).first()
        if deal and deal.stage != cfg["stage"]:
            ctx.session.add(DealStageEvent(organization_id=deal.organization_id, deal_id=deal.id,
                                           from_stage=deal.stage, to_stage=cfg["stage"], source="rule",
                                           reason=f"Journey «{ctx.journey.name}»"))
            deal.stage, deal.stage_changed_at = cfg["stage"], utcnow()
        return "next", nxt
    if t == "notify_agent":
        await _notify(ctx, cfg)
        return "next", nxt
    if t == "start_flow":
        await _start_flow(ctx, cfg)
        return "next", nxt
    if t == "exit":
        return "exit", cfg.get("reason") or "exit_step"
    return "next", nxt


# --- Envíos ----------------------------------------------------------------------------------------------------
async def _frequency_capped(ctx: Ctx) -> bool:
    cap = ctx.settings.get("frequency_cap") or {}
    for key, delta in (("per_day", timedelta(days=1)), ("per_week", timedelta(days=7))):
        limit = cap.get(key)
        if not limit:
            continue
        sent = await ctx.session.scalar(
            select(func.count()).select_from(Message).join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.contact_id == ctx.contact.id, Message.direction == "out",
                   Message.sender_type.in_(("journey", "campaign")), Message.status != "failed",
                   Message.created_at >= ctx.now - delta))
        if (sent or 0) >= int(limit):
            return True
    return False


async def _marketing_consent(ctx: Ctx) -> bool:
    if ctx.settings.get("require_consent") != "marketing":
        return True
    granted = await ctx.session.scalar(text(
        "select c.granted from public.contact_consents c where c.contact_id = :c and c.consent_type = 'marketing' "
        "and c.revoked_at is null order by c.recorded_at desc limit 1"), {"c": ctx.contact.id})
    return bool(granted)


async def _send_step(ctx: Ctx, step: dict) -> tuple[str, object]:
    sid, cfg, nxt = step["id"], step.get("config") or {}, step.get("next")
    if ctx.marked("sent", sid):  # reintento: este paso ya se intentó enviar (nunca dos veces)
        return "next", nxt
    tz = await org_tz(ctx.session, ctx.e.organization_id, (ctx.settings.get("quiet_hours") or {}).get("timezone"))
    quiet_end = in_quiet_hours(ctx.now, ctx.settings.get("quiet_hours"), tz)
    if quiet_end:
        return "wait", quiet_end
    marketing = bool(cfg.get("marketing", True))
    if marketing and ctx.contact.marketing_opt_out:
        return "exit", "opt_out"
    if marketing and not await _marketing_consent(ctx):
        ctx.mark("sent", sid, "skipped")
        await add_event(ctx.session, ctx.e, sid, "skipped", data={"reason": "no_consent"})
        return "next", nxt
    if await _frequency_capped(ctx):
        ctx.mark("sent", sid, "skipped")
        await add_event(ctx.session, ctx.e, sid, "skipped", data={"reason": "frequency_cap"})
        return "next", nxt
    # Marca ANTES de enviar y confirma: si el proceso muere después, el reintento no reenvía
    ctx.mark("sent", sid, "sending")
    await ctx.session.commit()
    if step["type"] == "send_template":
        msg, why = await _send_template(ctx, sid, cfg)
    elif step["type"] == "send_email":
        msg, why = await _send_email(ctx, sid, cfg)
    else:
        msg, why = await _send_auto(ctx, sid, cfg)
    if msg is None:
        ctx.mark("sent", sid, "skipped")
        await add_event(ctx.session, ctx.e, sid, "skipped", data={"reason": why})
    elif msg.status == "failed":
        ctx.mark("sent", sid, "failed")
        await add_event(ctx.session, ctx.e, sid, "failed", message_id=msg.id, data={"error": (msg.error or "")[:300]})
    else:
        ctx.mark("sent", sid, "sent")
        ctx.mark("sent_at", sid, ctx.now.isoformat())
        await add_event(ctx.session, ctx.e, sid, "sent", message_id=msg.id,
                        data={"channel": why, "template": cfg.get("template_name")})
    return "next", nxt


async def _whatsapp_channel(ctx: Ctx, channel_id: int | None) -> Channel | None:
    q = select(Channel).where(Channel.organization_id == ctx.e.organization_id, Channel.provider == "whatsapp_cloud")
    if channel_id:
        q = q.where(Channel.id == int(channel_id))
    return (await ctx.session.scalars(q.order_by(Channel.id).limit(1))).first()


async def _record(ctx: Ctx, conv: Conversation, msg: Message) -> Message:
    from app.service import record_message

    msg.journey_id = ctx.journey.id
    return await record_message(ctx.session, conv, msg)


async def _send_template(ctx: Ctx, sid: str, cfg: dict) -> tuple[Message | None, str]:
    from app import templates
    from app.identity import require_phone_for_template, wa_address
    from app.service import get_or_create_conversation, wa_client

    channel = await _whatsapp_channel(ctx, cfg.get("channel_id"))
    if channel is None:
        return None, "no_whatsapp_channel"
    if not wa_address(ctx.contact):
        return None, "no_whatsapp_address"
    catalog = await templates.list_templates(ctx.session, channel)
    tpl = next((t for t in catalog if t["name"] == cfg["template_name"] and t["language"] == cfg["language"]), None)
    if not tpl or tpl.get("status") != "APPROVED":
        return None, "template_not_approved"
    if tpl.get("category") == "MARKETING" and ctx.contact.marketing_opt_out:
        return None, "opt_out"
    conv = await get_or_create_conversation(ctx.session, channel, ctx.contact, reopen=False)
    values = templates.personalize([render(str(v), ctx.contact) for v in cfg.get("params") or []], ctx.contact.name)
    components, rendered = templates.build(tpl, values)
    msg = Message(direction="out", sender_type="journey", type="template", text=rendered, template_name=tpl["name"])
    try:
        require_phone_for_template(ctx.contact, tpl.get("category"))
        msg.wa_message_id = await (await wa_client(ctx.session, channel)).send_template(
            wa_address(ctx.contact), tpl["name"], tpl["language"], components)
        msg.status = "sent"
    except Exception as ex:  # noqa: BLE001
        msg.status, msg.error = "failed", str(ex)[:2000]
    return await _record(ctx, conv, msg), "whatsapp_template"


async def _send_text_on(ctx: Ctx, conv: Conversation, body: str) -> Message:
    from app.identity import wa_address
    from app.service import wa_client

    msg = Message(direction="out", sender_type="journey", type="text", text=body)
    try:
        client = await wa_client(ctx.session, conv.channel, conv)
        ids = await client.send_text(wa_address(conv.contact), body)
        msg.wa_message_id, msg.status = ids[0], "sent"
        if getattr(client, "last_meta", None):
            msg.type, msg.metadata_ = "email", client.last_meta
    except Exception as ex:  # noqa: BLE001
        msg.status, msg.error = "failed", str(ex)[:2000]
    return await _record(ctx, conv, msg)


async def _contact_email(ctx: Ctx) -> str | None:
    if ctx.contact.email:
        return str(ctx.contact.email).lower()
    return await ctx.session.scalar(text(
        "select primary_email from public.contact_golden where contact_id = :c"), {"c": ctx.contact.id})


async def _email_conversation(ctx: Ctx, channel_id: int | None) -> Conversation | None:
    from app.service import get_or_create_conversation

    q = select(Channel).where(Channel.organization_id == ctx.e.organization_id, Channel.provider == "email",
                              Channel.status == "active")
    if channel_id:
        q = q.where(Channel.id == int(channel_id))
    channel = (await ctx.session.scalars(q.order_by(Channel.id).limit(1))).first()
    if channel is None:
        return None
    ident = await ctx.session.scalar(select(ContactIdentity).where(
        ContactIdentity.channel_id == channel.id, ContactIdentity.contact_id == ctx.contact.id).limit(1))
    if ident is None:
        address = await _contact_email(ctx)
        if not address:
            return None
        taken = await ctx.session.scalar(select(ContactIdentity.contact_id).where(
            ContactIdentity.channel_id == channel.id, ContactIdentity.external_id == address))
        if taken and taken != ctx.contact.id:
            return None
        ctx.session.add(ContactIdentity(organization_id=channel.organization_id, contact_id=ctx.contact.id,
                                        provider="email", channel_id=channel.id, external_id=address))
        await ctx.session.flush()
    return await get_or_create_conversation(ctx.session, channel, ctx.contact, reopen=False)


async def _send_email(ctx: Ctx, sid: str, cfg: dict) -> tuple[Message | None, str]:
    from app.service import wa_client

    conv = await _email_conversation(ctx, cfg.get("channel_id"))
    if conv is None:
        return None, "no_email"
    body = tracked_text(render(str(cfg["body"]), ctx.contact), ctx.e, sid)
    msg = Message(direction="out", sender_type="journey", type="email", text=body)
    try:
        client = await wa_client(ctx.session, conv.channel, conv)
        msg.wa_message_id = await client.send_email(body, subject=render(str(cfg["subject"]), ctx.contact))
        msg.status, msg.metadata_ = "sent", client.last_meta
    except Exception as ex:  # noqa: BLE001
        msg.status, msg.error = "failed", str(ex)[:2000]
    return await _record(ctx, conv, msg), "email"


async def _send_auto(ctx: Ctx, sid: str, cfg: dict) -> tuple[Message | None, str]:
    """Texto por el primer canal disponible según la prioridad del journey."""
    from app.service import within_session_window

    body = tracked_text(render(str(cfg["text"]), ctx.contact), ctx.e, sid)
    chosen = cfg.get("channel") or "auto"
    order = [chosen] if chosen != "auto" else (ctx.settings.get("channels_priority")
                                                or ["whatsapp_cloud", "email", "webchat"])
    for provider in order:
        if provider == "whatsapp_cloud":
            channel = await _whatsapp_channel(ctx, cfg.get("channel_id"))
            if channel is None:
                continue
            conv = await ctx.session.scalar(select(Conversation).where(
                Conversation.contact_id == ctx.contact.id, Conversation.channel_id == channel.id)
                .order_by(Conversation.id.desc()).limit(1))
            if conv is not None and within_session_window(conv, human=False):
                return await _send_text_on(ctx, conv, body), "whatsapp_text"
            fb = cfg.get("fallback_template")
            if fb and fb.get("template_name"):  # fuera de la ventana de 24 h: plantilla aprobada
                return await _send_template(ctx, sid, {**fb, "channel_id": channel.id})
        elif provider == "email":
            conv = await _email_conversation(ctx, None)
            if conv is not None:
                return await _send_text_on(ctx, conv, body), "email"
        elif provider == "webchat":
            conv = await ctx.session.scalar(text(
                "select c.id from public.conversations c join public.channels ch on ch.id = c.channel_id "
                "join public.webchat_sessions w on w.conversation_id = c.id "
                "where c.contact_id = :c and ch.provider = 'webchat' and w.last_seen_at > now() - make_interval(secs => :s) "
                "order by w.last_seen_at desc limit 1"), {"c": ctx.contact.id, "s": WEBCHAT_ACTIVE_S})
            if conv:
                from app.service import reload

                c = await ctx.session.get(Conversation, conv)
                return await _send_text_on(ctx, await reload(ctx.session, c), body), "webchat"
    return None, "no_channel"


# --- Condiciones -------------------------------------------------------------------------------------------------
async def _branch(ctx: Ctx, step: dict) -> tuple[str, object]:
    sid, cond = step["id"], (step.get("config") or {}).get("condition") or {}
    branches = step.get("branches") or {}
    if cond.get("type") == "field":
        from app import segments

        ids = await segments.matching_ids(ctx.session, ctx.e.organization_id, cond.get("rule") or {},
                                          "k.id = :cid", {"cid": ctx.contact.id})
        return "next", branches.get("yes" if ids else "no")
    started = ctx.marked("branch_at", sid)
    if started is None:
        started = ctx.now.isoformat()
        ctx.mark("branch_at", sid, started)
    since = datetime.fromisoformat(started)
    # Desde el último envío del journey (la respuesta a ese mensaje cuenta aunque llegue antes de la condición)
    sent_times = [datetime.fromisoformat(v) for v in (ctx.context.get("sent_at") or {}).values()]
    window_start = max(sent_times) if sent_times else since
    kind = {"replied": "replied", "read": "read", "clicked": "clicked"}[cond["type"]]
    hit = await ctx.session.scalar(select(func.count()).select_from(JourneyEvent).where(
        JourneyEvent.enrollment_id == ctx.e.id, JourneyEvent.kind == kind, JourneyEvent.created_at >= window_start))
    if hit:
        return "next", branches.get("yes")
    deadline = since + timedelta(hours=float(cond.get("within_hours") or 24))
    if ctx.now >= deadline:
        return "next", branches.get("no")
    return "wait", deadline


# --- Acciones -----------------------------------------------------------------------------------------------------
async def _update_contact(ctx: Ctx, cfg: dict) -> None:
    from app.fields import coerce, fields_by_key, set_custom, set_native

    field, value = str(cfg["field"]), cfg.get("value")
    if isinstance(value, str):
        value = render(value, ctx.contact)
    if field in ("stage", "name", "email", "notes"):
        if field == "stage" and value not in ("lead", "prospect", "client", "lost"):
            raise ValueError(f"Etapa inválida: {value}")
        set_native(ctx.session, ctx.contact, field, value, "flow")
        return
    if field.startswith("custom:"):
        f = (await fields_by_key(ctx.session, ctx.e.organization_id)).get(field.split(":", 1)[1])
        if f is None:
            raise ValueError(f"Campo inexistente: {field}")
        await set_custom(ctx.session, ctx.contact, f, coerce(f, value), "api")
        return
    raise ValueError(f"Campo no editable: {field}")


async def _notify(ctx: Ctx, cfg: dict) -> None:
    target = cfg.get("to", "owner")
    agent_id = (ctx.contact.owner_agent_id if target == "owner" else ctx.contact.last_agent_id if target == "last_agent"
                else int(cfg.get("agent_id") or 0))
    if not agent_id or not await ctx.session.get(Agent, agent_id):
        return
    try:
        from app.notifications import notify

        await notify(ctx.session, agent_id, "system", render(str(cfg["title"]), ctx.contact),
                     render(str(cfg.get("body") or ""), ctx.contact) or None, f"/clientes?c={ctx.contact.id}",
                     {"journey_id": ctx.journey.id, "contact_id": ctx.contact.id})
    except (ImportError, AttributeError):  # pragma: no cover
        log.info("Notificaciones no disponibles")


async def _start_flow(ctx: Ctx, cfg: dict) -> None:
    from app.flows.engine import start_run
    from app.models import Flow, FlowVersion
    from app.service import get_or_create_conversation, reload

    flow = await ctx.session.get(Flow, int(cfg["flow_id"]))
    if flow is None or flow.organization_id != ctx.e.organization_id or not flow.current_version_id:
        raise ValueError("Flujo inexistente o sin publicar")
    channel = await _whatsapp_channel(ctx, cfg.get("channel_id"))
    if channel is None:
        raise ValueError("No hay número de WhatsApp para el flujo")
    conv = await reload(ctx.session, await get_or_create_conversation(ctx.session, channel, ctx.contact, reopen=False))
    version = await ctx.session.get(FlowVersion, flow.current_version_id)
    await start_run(ctx.session, flow, version, 0, conv, "manual", None)


# --- Metas y eventos externos --------------------------------------------------------------------------------------
async def goal_reached(session: AsyncSession, org: int, contact_id: int, event: str, data: dict | None = None) -> int:
    """Marca como meta cumplida las inscripciones activas cuyo objetivo coincide. Devuelve cuántas."""
    data = data or {}
    rows = (await session.scalars(select(JourneyEnrollment).where(
        JourneyEnrollment.organization_id == org, JourneyEnrollment.contact_id == contact_id,
        JourneyEnrollment.status.in_(ACTIVE)))).all()
    done = 0
    for e in rows:
        journey = await session.get(Journey, e.journey_id)
        goal = (journey.settings or {}).get("goal") if journey else None
        if not goal or goal.get("event") != event:
            continue
        if goal.get("window_days") and utcnow() - e.enrolled_at > timedelta(days=int(goal["window_days"])):
            continue
        if event == "stage" and (goal.get("pipeline") and goal["pipeline"] != data.get("pipeline")
                                 or goal.get("stage") and goal["stage"] != data.get("stage")):
            continue
        if event == "deal_won" and goal.get("pipeline") and goal["pipeline"] != data.get("pipeline"):
            continue
        await finish(session, e, "goal_met", event)
        done += 1
    if done:
        await session.commit()
    return done
