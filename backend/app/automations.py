"""Tareas automatizadas: bienvenida, respuestas y transferencias por palabra clave,
horario de atención y cierre por inactividad."""

import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, set_actor
from app.models import Automation, Conversation, Message, utcnow
from app.settings_store import get_setting

log = logging.getLogger(__name__)

TYPES = ("welcome", "keyword_reply", "keyword_handoff", "business_hours", "inactivity_close")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


def keyword_match(text: str, keywords: list[str], mode: str = "contains") -> str | None:
    t = _norm(text)
    for kw in keywords:
        k = _norm(kw)
        if k and (t == k if mode == "exact" else k in t):
            return kw
    return None


def within_hours(cfg: dict, tz_name: str, now: datetime | None = None) -> bool:
    now = (now or utcnow()).astimezone(ZoneInfo(cfg.get("timezone") or tz_name))
    if now.weekday() not in cfg.get("days", [0, 1, 2, 3, 4]):
        return False
    return cfg.get("start", "00:00") <= now.strftime("%H:%M") < cfg.get("end", "23:59")


async def _rules(session: AsyncSession, org: int, type_: str | None = None) -> list[Automation]:
    stmt = (select(Automation).where(Automation.organization_id == org, Automation.enabled)
            .order_by(Automation.priority, Automation.id))
    if type_:
        stmt = stmt.where(Automation.type == type_)
    return list((await session.scalars(stmt)).all())


async def on_inbound(session: AsyncSession, conv: Conversation, msg: Message, is_new_contact: bool) -> bool:
    """Se evalúa con cada mensaje del cliente mientras lo atiende el bot.
    Devuelve True si una regla ya respondió y el bot de IA no debe contestar."""
    from app.service import handoff, send_text

    if conv.status != "bot":
        return False
    text = msg.text or msg.transcript or ""
    for rule in await _rules(session, conv.organization_id):
        cfg = rule.config or {}
        if rule.type == "welcome" and is_new_contact and cfg.get("message"):
            await send_text(session, conv, cfg["message"], sender_type="bot")
        elif rule.type == "keyword_reply" and text and cfg.get("reply"):
            if keyword_match(text, cfg.get("keywords", []), cfg.get("match", "contains")):
                await send_text(session, conv, cfg["reply"], sender_type="bot")
                return True
        elif rule.type == "keyword_handoff" and text:
            kw = keyword_match(text, cfg.get("keywords", []), cfg.get("match", "contains"))
            if kw:
                if cfg.get("message"):
                    await send_text(session, conv, cfg["message"], sender_type="bot")
                await handoff(session, conv, f"Palabra clave: {kw}", cfg.get("group_id"), actor="automation")
                return True
    return False


async def after_handoff(session: AsyncSession, conv: Conversation) -> None:
    """Si la transferencia ocurre fuera del horario de atención, avisa al cliente."""
    from app.service import send_text

    tz = (await get_setting(session, "company", conv.organization_id))["timezone"]
    for rule in await _rules(session, conv.organization_id, "business_hours"):
        cfg = rule.config or {}
        if cfg.get("message") and not within_hours(cfg, tz):
            await send_text(session, conv, cfg["message"], sender_type="bot")
            return


async def close_inactive() -> int:
    from app.service import close, send_text, typification_by_name, within_session_window

    closed = 0
    async with SessionLocal() as session:
        rules = (await session.scalars(select(Automation).where(
            Automation.enabled, Automation.type == "inactivity_close"))).all()
        for rule in rules:
            hours = float((rule.config or {}).get("hours") or 0)
            if hours <= 0:
                continue
            cutoff = utcnow() - timedelta(hours=hours)
            convs = (await session.scalars(select(Conversation).where(
                Conversation.organization_id == rule.organization_id, Conversation.status != "closed",
                Conversation.last_message_at < cutoff))).unique().all()
            typ = await typification_by_name(session, rule.organization_id, "Inactividad")
            for conv in convs:
                message = (rule.config or {}).get("message")
                if message and within_session_window(conv):
                    await send_text(session, conv, message, sender_type="bot")
                await set_actor(session, "automation")
                await close(session, conv, typ, actor="automation")
                closed += 1
    return closed


async def inactivity_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            n = await close_inactive()
            if n:
                log.info("Cerradas %s conversaciones por inactividad", n)
        except Exception:
            log.exception("Falló el cierre por inactividad")
