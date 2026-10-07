"""Mensajes disparadores (wa_links): enlaces de WhatsApp con tracking. Ver docs/data-model.md §10.5.

Cada enlace se publica de dos formas:
- short_url  /t/l/{slug}: para web, redes, correo, QR y anuncios de Google (captura gclid/fbclid/UTMs de la URL).
- direct_url wa.me/{número}?text=…: para el mensaje prellenado de anuncios Click to WhatsApp de Meta (no admite
  redirecciones); se atribuye por el ID del anuncio (meta_ad_ids) o por el texto.
"""

import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import ad_enrichment as enrichment
from app.attribution import MIN_TRIGGER_KEY, find_ref_code, link_by_text, trigger_key
from app.auth import current_agent, require_admin
from app.config import get_settings
from app.db import get_session
from app.models import Agent, Channel, Flow, Group, WaLink, utcnow
from app.plans import feature_required
from app.routers.reports import _rows
from app.routers.tracking import direct_url, link_channel
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/wa-links", tags=["wa-links"], dependencies=[Depends(feature_required("attribution"))])
PLATFORMS = ("meta_ads", "google_ads", "web", "social", "email", "qr", "sms", "other")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
# UTMs sugeridos por plataforma (el usuario puede cambiarlos)
UTM_DEFAULTS = {
    "meta_ads": {"utm_source": "facebook", "utm_medium": "paid_social"},
    "google_ads": {"utm_source": "google", "utm_medium": "cpc"},
    "social": {"utm_source": "social", "utm_medium": "social"},
    "email": {"utm_source": "email", "utm_medium": "email"},
    "qr": {"utm_source": "qr", "utm_medium": "offline"},
    "sms": {"utm_source": "sms", "utm_medium": "sms"},
}


class LinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_text: str = Field(min_length=1, max_length=500)
    platform: str = "web"
    slug: str | None = None
    channel_id: int | None = None
    append_ref: bool = True
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    meta_ad_ids: list[str] = []
    google_campaign_ids: list[str] = []
    tags: list[str] = []
    group_id: int | None = None
    flow_id: int | None = None
    is_active: bool = True


class LinkOut(BaseModel):
    id: int
    name: str
    slug: str
    platform: str
    trigger_text: str
    append_ref: bool
    channel_id: int | None
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    utm_content: str | None
    utm_term: str | None
    meta_ad_ids: list[str]
    google_campaign_ids: list[str]
    tags: list[str]
    group_id: int | None
    flow_id: int | None
    is_active: bool
    short_url: str
    direct_url: str
    google_final_url_suffix: str
    stats: dict
    created_at: UTCDateTime
    updated_at: UTCDateTime


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v[:200] or None


def short_url(link: WaLink) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/t/l/{link.slug}"


def _out(link: WaLink, channel: Channel | None, stats: dict | None = None) -> LinkOut:
    utm = {f: getattr(link, f) for f in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")}
    # Sufijo de URL final para Google Ads: los UTMs del enlace + gclid lo agrega Google (auto-tagging)
    suffix = "&".join(f"{k}={v}" for k, v in utm.items() if v)
    return LinkOut(id=link.id, name=link.name, slug=link.slug, platform=link.platform, trigger_text=link.trigger_text,
                   append_ref=link.append_ref, channel_id=link.channel_id, **utm,
                   meta_ad_ids=list(link.meta_ad_ids or []), google_campaign_ids=list(link.google_campaign_ids or []),
                   tags=list(link.tags or []), group_id=link.group_id, flow_id=link.flow_id, is_active=link.is_active,
                   short_url=short_url(link), direct_url=direct_url(channel, link), google_final_url_suffix=suffix,
                   stats=stats or {"clicks": 0, "conversations": 0, "conversions": 0, "conversion_value": 0},
                   created_at=link.created_at, updated_at=link.updated_at)


async def _stats(session: AsyncSession, org: int, days: int = 30) -> dict[int, dict]:
    start = date.today() - timedelta(days=days - 1)
    rows = await _rows(session, """
        select link_id, sum(clicks)::int as clicks, sum(conversations)::int as conversations,
               sum(conversions)::int as conversions, coalesce(sum(conversion_value), 0)::float as conversion_value
        from reporting.daily_links where organization_id = :o and day >= :a group by link_id""", o=org, a=start)
    return {r.pop("link_id"): r for r in rows}


async def _validate(session: AsyncSession, org: int, body: LinkIn, link_id: int | None) -> dict:
    if body.platform not in PLATFORMS:
        raise HTTPException(422, f"Plataforma inválida: {body.platform}")
    text = body.trigger_text.strip()
    if find_ref_code(text):
        raise HTTPException(422, "El texto no debe incluir un código «ref:»; el sistema lo agrega solo")
    key = trigger_key(text)
    if len(key) < MIN_TRIGGER_KEY:
        raise HTTPException(422, "El mensaje es muy corto para identificar el origen: usa al menos 8 letras "
                                 "(ej. «Hola, quiero la promo CX-5»)")
    if body.is_active:
        clash = await session.scalar(select(WaLink).where(WaLink.organization_id == org, WaLink.is_active,
                                                          WaLink.trigger_key == key, WaLink.id != (link_id or 0)))
        if clash:
            raise HTTPException(409, f"Otro enlace activo usa el mismo mensaje: «{clash.name}». Cambia el texto "
                                     "para poder distinguirlos")
    for model, value, label in ((Channel, body.channel_id, "Número"), (Group, body.group_id, "Grupo"),
                                (Flow, body.flow_id, "Flujo")):
        if value is not None:
            row = await session.get(model, value)
            if not row or row.organization_id != org:
                raise HTTPException(404, f"{label} no encontrado")
    utm = {f: _clean(getattr(body, f)) for f in ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                                                  "utm_term")}
    for k, v in UTM_DEFAULTS.get(body.platform, {}).items():
        utm[k] = utm[k] or v
    utm["utm_campaign"] = utm["utm_campaign"] or (re.sub(r"[^a-z0-9]+", "_", trigger_key(body.name)).strip("_")
                                                  or None)
    return {"name": body.name.strip(), "trigger_text": text, "trigger_key": key, "platform": body.platform,
            "channel_id": body.channel_id, "append_ref": body.append_ref, **utm,
            "meta_ad_ids": sorted({a.strip() for a in body.meta_ad_ids if a.strip()}),
            "google_campaign_ids": sorted({c.strip().replace("-", "") for c in body.google_campaign_ids if c.strip()}),
            "tags": sorted({t.strip().lower() for t in body.tags if t.strip()}), "group_id": body.group_id,
            "flow_id": body.flow_id, "is_active": body.is_active}


async def _slug(session: AsyncSession, wanted: str | None, name: str, link_id: int | None = None) -> str:
    if wanted:
        slug = wanted.strip().lower()
        if not SLUG_RE.match(slug):
            raise HTTPException(422, "El slug solo admite minúsculas, números y guiones (2 a 48 caracteres)")
        taken = await session.scalar(select(WaLink.id).where(WaLink.slug == slug, WaLink.id != (link_id or 0)))
        if taken:
            raise HTTPException(409, "Ese slug ya está en uso")
        return slug
    base = re.sub(r"[^a-z0-9]+", "-", trigger_key(name)).strip("-")[:32] or "wa"
    for _ in range(10):
        slug = f"{base}-{secrets.token_hex(2)}"
        if not await session.scalar(select(WaLink.id).where(WaLink.slug == slug)):
            return slug
    raise HTTPException(503, "No se pudo generar el slug")


@router.get("", response_model=list[LinkOut])
async def list_links(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    links = (await session.scalars(select(WaLink).where(WaLink.organization_id == org)
                                   .order_by(WaLink.is_active.desc(), WaLink.name))).all()
    stats = await _stats(session, org)
    out = []
    for link in links:
        out.append(_out(link, await link_channel(session, link), stats.get(link.id)))
    return out


# --- Selectores de anuncios para el formulario (antes de /{link_id} para que no los capture) ---
@router.get("/meta-ads")
async def meta_ads(q: str | None = None, agent: Agent = Depends(current_agent),
                   session: AsyncSession = Depends(get_session)):
    """Anuncios de la cuenta publicitaria de Meta, para elegir los que usan este mensaje disparador."""
    org = agent.organization_id
    conn = await enrichment.connection(session, org, "meta")
    if not conn or conn.status != "connected":
        raise HTTPException(409, "Conecta Meta en Configuraciones → Conversiones para listar tus anuncios")
    account = str((conn.settings or {}).get("ad_account_id") or "").strip().removeprefix("act_")
    if not account:
        raise HTTPException(409, "Configura la cuenta publicitaria de Meta (ID de la cuenta de anuncios) en "
                                 "Configuraciones → Conversiones")
    token = await enrichment.meta_token(session, org)
    if not token:
        raise HTTPException(409, "Falta el token de Meta: agrégalo en Configuraciones → Conversiones")
    try:
        data = await enrichment.meta_get(f"act_{account}/ads", token, {
            "fields": "id,name,effective_status,campaign{id,name},adset{name}", "limit": 200})
    except enrichment.EnrichError as e:
        raise HTTPException(502, str(e)) from e
    needle = (q or "").strip().lower()
    out = []
    for ad in data.get("data") or []:
        campaign, adset = ad.get("campaign") or {}, ad.get("adset") or {}
        row = {"id": str(ad.get("id")), "name": ad.get("name"), "status": ad.get("effective_status"),
               "campaign_id": campaign.get("id"), "campaign_name": campaign.get("name"),
               "adset_name": adset.get("name")}
        haystack = " ".join(str(v or "") for v in row.values()).lower()
        if not needle or needle in haystack:
            out.append(row)
    return out


@router.get("/google-campaigns")
async def google_campaigns(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Campañas de Google Ads (no eliminadas) de la cuenta conectada."""
    conn = await enrichment.connection(session, agent.organization_id, "google_ads")
    if not conn or conn.status != "connected":
        raise HTTPException(409, "Conecta Google Ads en Configuraciones → Conversiones para listar tus campañas")
    try:
        results = await enrichment.google_search(
            session, conn, "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
                           "WHERE campaign.status != 'REMOVED' ORDER BY campaign.name")
    except enrichment.EnrichError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        await session.commit()  # el token renovado queda guardado
    return [{"id": str((r.get("campaign") or {}).get("id")), "name": (r.get("campaign") or {}).get("name"),
             "status": (r.get("campaign") or {}).get("status")} for r in results]


@router.get("/{link_id}", response_model=LinkOut)
async def get_link(link_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    link = await _get(session, agent.organization_id, link_id)
    stats = await _stats(session, agent.organization_id)
    return _out(link, await link_channel(session, link), stats.get(link.id))


@router.post("", response_model=LinkOut)
async def create_link(body: LinkIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    data = await _validate(session, org, body, None)
    link = WaLink(organization_id=org, slug=await _slug(session, body.slug, body.name), created_by=agent.id, **data)
    session.add(link)
    await _commit(session)
    return _out(link, await link_channel(session, link))


@router.put("/{link_id}", response_model=LinkOut)
async def update_link(link_id: int, body: LinkIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    link = await _get(session, agent.organization_id, link_id)
    data = await _validate(session, agent.organization_id, body, link.id)
    if body.slug and body.slug.strip().lower() != link.slug:
        # Cambiar el slug rompe los enlaces ya publicados: solo se permite explícitamente
        link.slug = await _slug(session, body.slug, body.name, link.id)
    for k, v in data.items():
        setattr(link, k, v)
    link.updated_at = utcnow()
    await _commit(session)
    stats = await _stats(session, agent.organization_id)
    return _out(link, await link_channel(session, link), stats.get(link.id))


@router.delete("/{link_id}")
async def delete_link(link_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Las atribuciones conservan sus UTMs; solo pierden la referencia al enlace."""
    link = await _get(session, agent.organization_id, link_id)
    await session.delete(link)
    await session.commit()
    return {"ok": True}


class TestIn(BaseModel):
    text: str


@router.post("/test-match")
async def test_match(body: TestIn, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Qué enlace reconocería este mensaje (para probar textos antes de publicar un anuncio)."""
    link = await link_by_text(session, agent.organization_id, body.text)
    return {"key": trigger_key(body.text), "ref_code": find_ref_code(body.text),
            "link": {"id": link.id, "name": link.name, "slug": link.slug} if link else None}


async def _get(session: AsyncSession, org: int, link_id: int) -> WaLink:
    link = await session.get(WaLink, link_id)
    if not link or link.organization_id != org:
        raise HTTPException(404, "Enlace no encontrado")
    return link


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as e:  # carrera con otro guardado del mismo texto o slug
        await session.rollback()
        raise HTTPException(409, "Ya existe un enlace activo con ese mensaje o ese slug") from e
