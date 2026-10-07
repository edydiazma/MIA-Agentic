"""Atribución de conversaciones con mensajes disparadores y toques. Ver docs/data-model.md §10.1 y §10.5.

Cada mensaje entrante se revisa buscando una señal de origen, en este orden:
1. Referral de Meta (Click to WhatsApp) → meta_ctwa (ctwa_clid, ad_id). El enlace se busca por el anuncio
   (wa_links.meta_ad_ids) o, si no, por el texto prellenado del anuncio.
2. Código de referencia "(ref: XXXXXX)" → visita web (script del sitio o enlace corto /t/l/{slug}):
   google_ads / meta_ads_web / paid_other / organic_web, con los UTMs y click ids de la visita.
3. Texto del mensaje disparador (aunque el cliente haya borrado el código) → el enlace y sus UTMs.
Sin señal, el primer mensaje de la conversación se atribuye a la campaña de plantilla que la abrió o a direct.

Modelo: la fila de `attributions` es el ÚLTIMO toque (lo que se sube como conversión); cada señal queda además
en `attribution_touches` (primer toque, regresos por otro anuncio). Un toque idéntico al anterior no se repite.
"""

import hashlib
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import any_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    Attribution,
    AttributionTouch,
    Conversation,
    IntegrationConnection,
    Message,
    WaLink,
    WebSession,
    utcnow,
)

# Base32 sin caracteres ambiguos (0/O, 1/I/L)
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REF_LEN = 6
REF_WINDOW = timedelta(days=90)
REF_RE = re.compile(r"\(?\s*ref\s*[:#\-]?\s*([A-Za-z0-9]{6})\b\s*\)?", re.IGNORECASE)
PAID_MEDIUMS = {"cpc", "ppc", "paid", "paid_social", "paidsocial", "cpm", "display", "paid-social"}
MIN_TRIGGER_KEY = 8  # textos más cortos ("hola") no identifican un enlace

SESSION_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "gbraid", "wbraid",
                  "fbc", "fbp")
UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
VALUE_FIELDS = (*SESSION_FIELDS, "ctwa_clid", "ad_id", "landing_url")
ENRICHED_FIELDS = ("platform_campaign_id", "platform_campaign_name", "ad_group_id", "ad_group_name", "ad_name",
                   "keyword")


def new_ref_code() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(REF_LEN))


def find_ref_code(value: str | None) -> str | None:
    if not value:
        return None
    for m in REF_RE.finditer(value):
        code = m.group(1).upper()
        if all(c in REF_ALPHABET for c in code):
            return code
    return None


def trigger_key(value: str | None) -> str:
    """Normaliza un texto disparador: sin código de referencia, tildes, signos ni espacios repetidos."""
    s = REF_RE.sub(" ", value or "")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


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


def channel_for_link(link: WaLink) -> str:
    """Canal cuando solo se reconoce el texto del enlace (sin visita ni referral)."""
    if link.platform == "meta_ads":
        return "meta_ctwa"
    if link.platform == "google_ads":
        return "google_ads"
    if link.platform in ("qr", "sms"):
        return "offline"
    return "paid_other" if (link.utm_medium or "").strip().lower() in PAID_MEDIUMS else "organic_web"


async def find_session(session: AsyncSession, org: int, code: str) -> WebSession | None:
    return (await session.scalars(
        select(WebSession).where(WebSession.organization_id == org, WebSession.ref_code == code,
                                 WebSession.created_at >= utcnow() - REF_WINDOW)
        .order_by(WebSession.created_at.desc()).limit(1))).first()


async def link_by_text(session: AsyncSession, org: int, value: str | None) -> WaLink | None:
    """El enlace activo cuyo texto disparador está contenido en el mensaje (el más largo gana)."""
    key = trigger_key(value)
    if len(key) < MIN_TRIGGER_KEY:
        return None
    return (await session.scalars(
        select(WaLink).where(WaLink.organization_id == org, WaLink.is_active,
                             func.length(WaLink.trigger_key) >= MIN_TRIGGER_KEY,
                             func.strpos(key, WaLink.trigger_key) > 0)
        .order_by(func.length(WaLink.trigger_key).desc()).limit(1))).first()


async def link_by_ad(session: AsyncSession, org: int, ad_id: str | None) -> WaLink | None:
    if not ad_id:
        return None
    return (await session.scalars(
        select(WaLink).where(WaLink.organization_id == org, WaLink.is_active, ad_id == any_(WaLink.meta_ad_ids))
        .limit(1))).first()


@dataclass
class Signal:
    """Origen detectado en un mensaje."""

    channel: str
    matched_by: str
    link: WaLink | None = None
    web_session: WebSession | None = None
    values: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.matched_by, getattr(self.link, "id", None), getattr(self.web_session, "id", None),
                self.values.get("ad_id"), self.values.get("ctwa_clid"))


async def detect(session: AsyncSession, org: int, value: str | None, referral: dict | None,
                 provider: str = "whatsapp_cloud") -> Signal | None:
    if referral:
        ad_id = referral.get("source_id")
        link = await link_by_ad(session, org, ad_id) or await link_by_text(session, org, value)
        # Anuncios que abren Messenger o Instagram Direct: el canal es la red; el anuncio queda en ad_id
        sig = Signal(provider if provider in ("messenger", "instagram") else "meta_ctwa", "ctwa_referral", link=link,
                     values={"ctwa_clid": referral.get("ctwa_clid"), "ad_id": ad_id,
                             "landing_url": referral.get("source_url")})
        if link:
            sig.values.update({f: getattr(link, f) for f in UTM_FIELDS if getattr(link, f)})
        return sig

    code = find_ref_code(value)
    if code:
        ws = await find_session(session, org, code)
        if ws:
            link = await session.get(WaLink, ws.link_id) if ws.link_id else None
            if link and link.organization_id != org:
                link = None
            vals = {f: getattr(ws, f) for f in SESSION_FIELDS}
            if not vals.get("fbc") and ws.fbclid:  # fbc derivado del fbclid (formato de Meta)
                vals["fbc"] = f"fb.1.{int(ws.created_at.timestamp() * 1000)}.{ws.fbclid}"
            if link:  # la visita manda; el enlace completa los UTMs que no traía la URL
                for f in UTM_FIELDS:
                    vals[f] = vals.get(f) or getattr(link, f)
            vals["landing_url"] = ws.landing_url
            return Signal(channel_for_session(ws), "ref_code", link=link, web_session=ws, values=vals)

    link = await link_by_text(session, org, value)
    if link:
        return Signal(channel_for_link(link), "trigger_text", link=link,
                      values={f: getattr(link, f) for f in UTM_FIELDS if getattr(link, f)})
    return None


async def _first_without_signal(session: AsyncSession, conv: Conversation,
                                provider: str = "whatsapp_cloud") -> Signal:
    last_out = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.direction == "out",
                              Message.sender_type != "system").order_by(Message.created_at.desc()).limit(1))).first()
    if last_out is not None and (last_out.campaign_id or last_out.sender_type == "campaign"):
        return Signal("campaign", "campaign",
                      values={"utm_campaign": f"campaign:{last_out.campaign_id}" if last_out.campaign_id else None})
    if provider in ("messenger", "instagram", "webchat"):
        return Signal(provider, "none")  # llegó por el canal sin anuncio ni enlace rastreado
    return Signal("direct", "none")


async def _enrichment_status(session: AsyncSession, org: int, values: dict) -> str:
    """pending si hay un anuncio de Meta o un gclid y la cuenta publicitaria está conectada."""
    wanted = []
    if values.get("ad_id"):
        wanted.append("meta")
    if values.get("gclid"):
        wanted.append("google_ads")
    if not wanted:
        return "skipped"
    connected = await session.scalar(select(func.count()).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider.in_(wanted),
        IntegrationConnection.status == "connected"))
    return "pending" if connected else "skipped"


def _apply(attr: Attribution, sig: Signal) -> None:
    """Escribe el toque en la fila de atribución (último toque)."""
    for f in VALUE_FIELDS:
        setattr(attr, f, sig.values.get(f))
    attr.channel, attr.matched_by = sig.channel, sig.matched_by
    attr.link_id = getattr(sig.link, "id", None)
    attr.web_session_id = getattr(sig.web_session, "id", None)
    attr.web_session_at = getattr(sig.web_session, "created_at", None)
    for f in ENRICHED_FIELDS:  # los nombres de campaña del toque anterior ya no aplican
        setattr(attr, f, None)
    attr.enriched_at = None


def _touch(conv: Conversation, msg: Message | None, sig: Signal, first: bool) -> AttributionTouch:
    v = sig.values
    return AttributionTouch(organization_id=conv.organization_id, conversation_id=conv.id,
                            contact_id=conv.contact_id, message_id=getattr(msg, "id", None), channel=sig.channel,
                            matched_by=sig.matched_by, link_id=getattr(sig.link, "id", None),
                            web_session_id=getattr(sig.web_session, "id", None), utm_source=v.get("utm_source"),
                            utm_medium=v.get("utm_medium"), utm_campaign=v.get("utm_campaign"), gclid=v.get("gclid"),
                            ctwa_clid=v.get("ctwa_clid"), ad_id=v.get("ad_id"), is_first=first)


async def attribute(session: AsyncSession, conv: Conversation, value: str | None, referral: dict | None) -> Attribution:
    """Calcula (sin guardar) la atribución de una conversación nueva."""
    sig = await detect(session, conv.organization_id, value, referral) or await _first_without_signal(session, conv)
    attr = Attribution(organization_id=conv.organization_id, conversation_id=conv.id, contact_id=conv.contact_id)
    _apply(attr, sig)
    return attr


async def on_inbound(session: AsyncSession, conv: Conversation, msg: Message, raw_message: dict,
                     provider: str = "whatsapp_cloud") -> WaLink | None:
    """Hook de ingest para cada mensaje entrante. Devuelve el enlace (mensaje disparador) que trajo este mensaje,
    para que el motor de flujos inicie el flujo del enlace; None si el mensaje no trae una señal nueva."""
    org = conv.organization_id
    value = msg.text or msg.transcript
    if raw_message.get("ref_code"):  # chat web: el widget envía el código de la visita (script de tracking)
        value = f"{value or ''} (ref: {raw_message['ref_code']})"
    sig = await detect(session, org, value, raw_message.get("referral"), provider)
    attr = (await session.scalars(select(Attribution).where(Attribution.conversation_id == conv.id))).first()
    now = utcnow()

    if attr is None:
        sig = sig or await _first_without_signal(session, conv, provider)
        attr = Attribution(organization_id=org, conversation_id=conv.id, contact_id=conv.contact_id, touches=1,
                           last_touch_at=now)
        _apply(attr, sig)
        attr.enrichment_status = await _enrichment_status(session, org, sig.values)
        session.add(attr)
        touch = _touch(conv, msg, sig, first=True)
        session.add(touch)
        await session.flush()
        attr.first_touch_id = touch.id
    elif sig is not None:
        last = (await session.scalars(
            select(AttributionTouch).where(AttributionTouch.conversation_id == conv.id)
            .order_by(AttributionTouch.occurred_at.desc()).limit(1))).first()
        if last is not None and (last.matched_by, last.link_id, last.web_session_id, last.ad_id,
                                 last.ctwa_clid) == sig.key():
            sig = None  # el cliente repite el mismo origen: no es un toque nuevo
        else:
            _apply(attr, sig)
            attr.enrichment_status = await _enrichment_status(session, org, sig.values)
            attr.touches = (attr.touches or 1) + 1
            attr.last_touch_at = now
            session.add(_touch(conv, msg, sig, first=False))
    if sig and sig.web_session is not None and sig.web_session.matched_conversation_id is None:
        sig.web_session.matched_conversation_id, sig.web_session.matched_at = conv.id, now
    await session.commit()
    if sig:  # toque nuevo: el CRM recibe la atribución actualizada (nunca debe romper el ingreso del mensaje)
        try:
            from app.crm.sync import enqueue_attribution
            from app.db import SessionLocal

            async with SessionLocal() as crm_session:  # sesión propia: un fallo no toca la del mensaje
                await enqueue_attribution(crm_session, conv.contact_id)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("No se pudo encolar la atribución al CRM", exc_info=True)

    if sig and attr.enrichment_status == "pending":
        from app.ad_enrichment import enrich_soon  # nombres de campaña/anuncio en segundo plano

        enrich_soon(attr.id)
    if sig and sig.link is not None:
        await apply_link_actions(session, conv, sig.link)
        return sig.link
    return None


async def apply_link_actions(session: AsyncSession, conv: Conversation, link: WaLink) -> None:
    """Etiquetas y grupo del enlace (el flujo lo inicia el motor de flujos)."""
    from app.db import set_actor
    from app.service import commit_and_broadcast, set_conversation_tags

    changed = False
    if link.tags:
        await session.refresh(conv, ["tag_links"])
        await set_actor(session, "system")
        changed = bool(await set_conversation_tags(session, conv, list(link.tags), "rule", replace=False))
    if link.group_id and conv.group_id != link.group_id:
        conv.group_id = link.group_id
        changed = True
    if changed:
        await commit_and_broadcast(session, conv)


async def touches_for(session: AsyncSession, conversation_id: int) -> list[AttributionTouch]:
    return list((await session.scalars(
        select(AttributionTouch).where(AttributionTouch.conversation_id == conversation_id)
        .order_by(AttributionTouch.occurred_at))).all())
