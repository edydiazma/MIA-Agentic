"""Atribución de conversaciones (último toque). Ver docs/data-model.md §10.1.

Orden de prioridad al llegar el primer mensaje del cliente en una conversación:
1. Referral de Meta (Click to WhatsApp) → meta_ctwa (ctwa_clid, ad_id).
2. Código de referencia "(ref: XXXXXX)" en el texto → sesión web: google_ads / meta_ads_web / paid_other / organic_web.
3. La conversación viene de una plantilla de campaña → campaign.
4. Ninguno → direct.
"""

import hashlib
import re
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Attribution, Conversation, Message, WebSession, utcnow

# Base32 sin caracteres ambiguos (0/O, 1/I/L)
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REF_LEN = 6
REF_WINDOW = timedelta(days=90)
REF_RE = re.compile(r"\(?\s*ref\s*[:#\-]?\s*([A-Za-z0-9]{6})\b\s*\)?", re.IGNORECASE)
PAID_MEDIUMS = {"cpc", "ppc", "paid", "paid_social", "paidsocial", "cpm", "display", "paid-social"}

SESSION_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "gbraid", "wbraid",
                  "fbc", "fbp")


def new_ref_code() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(REF_LEN))


def find_ref_code(text: str | None) -> str | None:
    if not text:
        return None
    for m in REF_RE.finditer(text):
        code = m.group(1).upper()
        if all(c in REF_ALPHABET for c in code):
            return code
    return None


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256((ip + get_settings().tracking_ip_salt).encode()).hexdigest()


def channel_for_session(s: WebSession) -> str:
    if s.gclid or s.gbraid or s.wbraid:
        return "google_ads"
    if s.fbclid or s.fbc:
        return "meta_ads_web"
    if (s.utm_medium or "").strip().lower() in PAID_MEDIUMS:
        return "paid_other"
    return "organic_web"


async def find_session(session: AsyncSession, org: int, code: str) -> WebSession | None:
    return (await session.scalars(
        select(WebSession).where(WebSession.organization_id == org, WebSession.ref_code == code,
                                 WebSession.created_at >= utcnow() - REF_WINDOW)
        .order_by(WebSession.created_at.desc()).limit(1))).first()


async def attribute(session: AsyncSession, conv: Conversation, text: str | None, referral: dict | None) -> Attribution:
    """Calcula (sin guardar) la atribución de la conversación."""
    attr = Attribution(organization_id=conv.organization_id, conversation_id=conv.id, contact_id=conv.contact_id,
                       channel="direct", matched_by="none")
    if referral:
        attr.channel, attr.matched_by = "meta_ctwa", "ctwa_referral"
        attr.ctwa_clid = referral.get("ctwa_clid")
        attr.ad_id = referral.get("source_id")
        attr.landing_url = referral.get("source_url")
        return attr

    code = find_ref_code(text)
    if code:
        ws = await find_session(session, conv.organization_id, code)
        if ws:
            attr.channel, attr.matched_by = channel_for_session(ws), "ref_code"
            attr.web_session_id, attr.web_session_at = ws.id, ws.created_at
            attr.landing_url = ws.landing_url
            for f in SESSION_FIELDS:
                setattr(attr, f, getattr(ws, f))
            if not attr.fbc and ws.fbclid:  # fbc derivado del fbclid (formato de Meta)
                attr.fbc = f"fb.1.{int(ws.created_at.timestamp() * 1000)}.{ws.fbclid}"
            ws.matched_conversation_id, ws.matched_at = conv.id, utcnow()
            return attr

    last_out = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.direction == "out",
                              Message.sender_type != "system").order_by(Message.created_at.desc()).limit(1))).first()
    if last_out is not None and (last_out.campaign_id or last_out.sender_type == "campaign"):
        attr.channel, attr.matched_by = "campaign", "campaign"
        attr.utm_campaign = f"campaign:{last_out.campaign_id}" if last_out.campaign_id else None
    return attr


async def on_inbound(session: AsyncSession, conv: Conversation, msg: Message, raw_message: dict) -> None:
    """Hook de ingest: atribuye la conversación con su primer mensaje entrante."""
    if await session.scalar(select(Attribution.id).where(Attribution.conversation_id == conv.id)):
        return
    attr = await attribute(session, conv, msg.text or msg.transcript, raw_message.get("referral"))
    session.add(attr)
    await session.commit()
