"""Atribución: sitios de tracking, conexiones de Google Ads / Meta (CAPI) y reportes de tráfico y atribución."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.config import get_settings
from app.conversions import GOOGLE_ADS_API, GOOGLE_TOKEN_URL, google_access_token
from app.db import get_session
from app.models import (
    Agent,
    Attribution,
    Channel,
    ConversionEvent,
    ConversionUpload,
    Conversation,
    IntegrationConnection,
    TrackingSite,
    Typification,
    WebEvent,
    WebSession,
    utcnow,
)
from app.plans import feature_required
from app.routers.reports import _days, _pct, _range
from app.routers.tracking import script
from app.schemas import UTCDateTime
from app.secrets_vault import delete_secret, put_secret

router = APIRouter(prefix="/api", tags=["attribution"])
# El plan debe incluir atribución (todas las rutas autenticadas; el callback de Google no tiene sesión)
FEATURE = [Depends(feature_required("attribution"))]
env = get_settings()
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/adwords"
CHANNEL_LABELS = {"meta_ctwa": "Click to WhatsApp (Meta)", "google_ads": "Google Ads", "meta_ads_web": "Meta Ads (web)",
                  "paid_other": "Otros pagos", "organic_web": "Web orgánica", "campaign": "Campañas WhatsApp",
                  "direct": "Directo"}


# --- Sitios de tracking --------------------------------------------------------
class SiteIn(BaseModel):
    name: str
    allowed_domains: list[str] = []
    channel_id: int | None = None
    wa_prefill: str = "Hola, quiero más información"
    is_active: bool = True


class SiteOut(BaseModel):
    id: int
    name: str
    public_key: str
    allowed_domains: list[str]
    channel_id: int | None
    wa_prefill: str
    is_active: bool
    created_at: UTCDateTime
    script_url: str
    snippet: str
    gtm_snippet: str
    wa_link: str
    sessions_7d: int = 0
    clicks_7d: int = 0


def _clean_domain(d: str) -> str:
    d = d.strip().lower()
    if "://" in d:
        d = urlparse(d).hostname or ""
    return d.strip("/").lstrip(".")


def site_out(site: TrackingSite, sessions: int = 0, clicks: int = 0) -> SiteOut:
    base = env.public_base_url.rstrip("/")
    src = f"{base}/t/{site.public_key}.js"
    snippet = f'<script async src="{src}"></script>'
    gtm = ("<script>\n(function(){var s=document.createElement('script');s.async=true;\n"
           f"s.src='{src}';document.head.appendChild(s);}})();\n</script>")
    return SiteOut(id=site.id, name=site.name, public_key=site.public_key, allowed_domains=site.allowed_domains or [],
                   channel_id=site.channel_id, wa_prefill=site.wa_prefill, is_active=site.is_active,
                   created_at=site.created_at, script_url=src, snippet=snippet, gtm_snippet=gtm,
                   wa_link=f"{base}/t/wa/{site.public_key}", sessions_7d=sessions, clicks_7d=clicks)


async def _site(session: AsyncSession, site_id: int, org: int) -> TrackingSite:
    site = await session.get(TrackingSite, site_id)
    if not site or site.organization_id != org:
        raise HTTPException(404, "Sitio no encontrado")
    return site


async def _validate_site(session: AsyncSession, body: SiteIn, org: int) -> dict:
    if not body.name.strip():
        raise HTTPException(422, "El nombre es obligatorio")
    if body.channel_id:
        ch = await session.get(Channel, body.channel_id)
        if not ch or ch.organization_id != org:
            raise HTTPException(422, "Número de WhatsApp inválido")
    data = body.model_dump()
    data["name"] = body.name.strip()
    data["allowed_domains"] = sorted({_clean_domain(d) for d in body.allowed_domains if _clean_domain(d)})
    data["wa_prefill"] = (body.wa_prefill or "").strip()[:500] or "Hola, quiero más información"
    return data


@router.get("/tracking-sites", response_model=list[SiteOut], dependencies=FEATURE)
async def list_sites(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    sites = (await session.scalars(select(TrackingSite).where(TrackingSite.organization_id == agent.organization_id)
                                   .order_by(TrackingSite.id))).all()
    since = utcnow() - timedelta(days=7)
    counts = dict((await session.execute(
        select(WebSession.site_id, func.count()).where(WebSession.organization_id == agent.organization_id,
                                                       WebSession.created_at >= since).group_by(WebSession.site_id))).all())
    clicks = dict((await session.execute(
        select(WebSession.site_id, func.count()).where(WebSession.organization_id == agent.organization_id,
                                                       WebSession.created_at >= since,
                                                       WebSession.wa_click_at.is_not(None))
        .group_by(WebSession.site_id))).all())
    return [site_out(s, counts.get(s.id, 0), clicks.get(s.id, 0)) for s in sites]


@router.post("/tracking-sites", response_model=SiteOut, dependencies=FEATURE)
async def create_site(body: SiteIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    data = await _validate_site(session, body, agent.organization_id)
    if await session.scalar(select(TrackingSite.id).where(TrackingSite.organization_id == agent.organization_id,
                                                          TrackingSite.name == data["name"])):
        raise HTTPException(409, "Ya existe un sitio con ese nombre")
    site = TrackingSite(organization_id=agent.organization_id, **data)
    session.add(site)
    await session.commit()
    await session.refresh(site)
    return site_out(site)


@router.put("/tracking-sites/{site_id}", response_model=SiteOut, dependencies=FEATURE)
async def update_site(site_id: int, body: SiteIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    site = await _site(session, site_id, agent.organization_id)
    for k, v in (await _validate_site(session, body, agent.organization_id)).items():
        setattr(site, k, v)
    await session.commit()
    return site_out(site)


@router.delete("/tracking-sites/{site_id}", dependencies=FEATURE)
async def delete_site(site_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Se desactiva (las sesiones históricas se conservan para los reportes)."""
    site = await _site(session, site_id, agent.organization_id)
    site.is_active = False
    await session.commit()
    return {"ok": True}


@router.get("/tracking-sites/{site_id}/script", dependencies=FEATURE)
async def site_script(site_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    site = await _site(session, site_id, agent.organization_id)
    return {"script": script(site)}


# --- Conexiones: Google Ads (OAuth) y Meta (token de sistema) -------------------
class GoogleAdsSettings(BaseModel):
    customer_id: str | None = None
    login_customer_id: str | None = None


class MetaConnectionIn(BaseModel):
    access_token: str | None = None
    waba_id: str | None = None


async def _conn(session: AsyncSession, org: int, provider: str) -> IntegrationConnection | None:
    return (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider == provider))).first()


def _conn_out(provider: str, c: IntegrationConnection | None) -> dict:
    base = {"provider": provider, "connected": bool(c and c.status == "connected"), "status": c.status if c else None,
            "external_account_id": c.external_account_id if c else None, "settings": (c.settings or {}) if c else {},
            "last_error": c.last_error if c else None, "has_token": bool(c and c.access_token_secret_id)}
    if provider == "google_ads":
        base["configured"] = bool(env.google_oauth_client_id and env.google_oauth_client_secret)
        base["developer_token"] = bool(env.google_ads_developer_token)
    else:
        base["configured"] = True
        base["server_token"] = bool(env.meta_capi_token)
    return base


@router.get("/attribution/connections", dependencies=FEATURE)
async def connections(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return [_conn_out(p, await _conn(session, agent.organization_id, p)) for p in ("google_ads", "meta")]


def _redirect_uri() -> str:
    return f"{env.public_base_url.rstrip('/')}/api/attribution/oauth/google_ads/callback"


@router.get("/attribution/connect/google_ads", dependencies=FEATURE)
async def connect_google(agent: Agent = Depends(require_admin)):
    if not (env.google_oauth_client_id and env.google_oauth_client_secret):
        raise HTTPException(422, "Configura GOOGLE_OAUTH_CLIENT_ID y GOOGLE_OAUTH_CLIENT_SECRET en el servidor")
    state = jwt.encode({"org": agent.organization_id, "agent": agent.id, "p": "google_ads",
                        "exp": utcnow() + timedelta(minutes=10)}, env.jwt_secret, algorithm="HS256")
    params = {"client_id": env.google_oauth_client_id, "redirect_uri": _redirect_uri(), "response_type": "code",
              "scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent", "state": state}
    return {"url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}


@router.get("/attribution/oauth/google_ads/callback")
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                          session: AsyncSession = Depends(get_session)):
    target = f"{env.frontend_base_url.rstrip('/')}/configuraciones/conversiones"
    if error or not code or not state:
        return RedirectResponse(f"{target}?error={error or 'cancelado'}")
    try:
        claims = jwt.decode(state, env.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return RedirectResponse(f"{target}?error=estado_invalido")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={
            "code": code, "client_id": env.google_oauth_client_id, "client_secret": env.google_oauth_client_secret,
            "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"})
    if r.status_code >= 400:
        return RedirectResponse(f"{target}?error=token")
    data = r.json()
    org = int(claims["org"])
    conn = await _conn(session, org, "google_ads")
    if not conn:
        conn = IntegrationConnection(organization_id=org, provider="google_ads", connected_by=int(claims["agent"]))
        session.add(conn)
        await session.flush()
    conn.access_token_secret_id = await put_secret(session, data["access_token"], f"google_ads_access:{conn.id}",
                                                   conn.access_token_secret_id)
    if data.get("refresh_token"):
        conn.refresh_token_secret_id = await put_secret(session, data["refresh_token"], f"google_ads_refresh:{conn.id}",
                                                        conn.refresh_token_secret_id)
    conn.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
    conn.scopes, conn.status, conn.last_error = [GOOGLE_SCOPE], "connected", None
    await session.commit()
    return RedirectResponse(f"{target}?connected=google_ads")


@router.get("/attribution/google_ads/customers", dependencies=FEATURE)
async def google_customers(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, "google_ads")
    if not conn:
        raise HTTPException(409, "Conecta Google Ads primero")
    token = await google_access_token(session, conn)
    await session.commit()
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{GOOGLE_ADS_API}/customers:listAccessibleCustomers",
                           headers={"Authorization": f"Bearer {token}", "developer-token": env.google_ads_developer_token})
    if r.status_code >= 400:
        raise HTTPException(502, f"Google Ads respondió {r.status_code}: {r.text[:300]}")
    return [rn.split("/")[-1] for rn in r.json().get("resourceNames", [])]


@router.put("/attribution/connections/google_ads", dependencies=FEATURE)
async def google_settings(body: GoogleAdsSettings, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, "google_ads")
    if not conn:
        raise HTTPException(409, "Conecta Google Ads primero")
    digits = {k: (v or "").replace("-", "").strip() or None for k, v in body.model_dump().items()}
    conn.settings = {**(conn.settings or {}), "login_customer_id": digits["login_customer_id"]}
    conn.external_account_id = digits["customer_id"]
    await session.commit()
    return _conn_out("google_ads", conn)


@router.put("/attribution/connections/meta", dependencies=FEATURE)
async def meta_settings(body: MetaConnectionIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    conn = await _conn(session, agent.organization_id, "meta")
    if not conn:
        conn = IntegrationConnection(organization_id=agent.organization_id, provider="meta", connected_by=agent.id)
        session.add(conn)
        await session.flush()
    if body.access_token:
        conn.access_token_secret_id = await put_secret(session, body.access_token.strip(), f"meta_capi:{conn.id}",
                                                       conn.access_token_secret_id)
    if body.waba_id is not None:
        conn.external_account_id = body.waba_id.strip() or None
    conn.status, conn.last_error = "connected", None
    await session.commit()
    return _conn_out("meta", conn)


@router.delete("/attribution/connections/{provider}", dependencies=FEATURE)
async def disconnect(provider: str, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if provider not in ("google_ads", "meta"):
        raise HTTPException(404, "Proveedor desconocido")
    conn = await _conn(session, agent.organization_id, provider)
    if conn:
        await delete_secret(session, conn.access_token_secret_id)
        await delete_secret(session, conn.refresh_token_secret_id)
        await session.delete(conn)
        await session.commit()
    return {"ok": True}


# --- Reportes --------------------------------------------------------------------
def _source(s: WebSession) -> str:
    if s.utm_source:
        return s.utm_source.lower()
    if s.gclid or s.gbraid or s.wbraid:
        return "google"
    if s.fbclid:
        return "facebook"
    host = urlparse(s.referrer).hostname if s.referrer else None
    return (host or "(directo)").removeprefix("www.")


def _medium(s: WebSession) -> str:
    if s.utm_medium:
        return s.utm_medium.lower()
    if s.gclid or s.gbraid or s.wbraid or s.fbclid:
        return "cpc"
    return "referral" if s.referrer else "(none)"


@router.get("/reports/web-traffic", dependencies=FEATURE)
async def web_traffic(start: str | None = None, end: str | None = None, site_id: int | None = None,
                      agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, d0, d1 = await _range(session, org, date.fromisoformat(start) if start else None,
                                      date.fromisoformat(end) if end else None)
    stmt = select(WebSession).where(WebSession.organization_id == org, WebSession.created_at >= lo,
                                    WebSession.created_at < hi)
    if site_id:
        stmt = stmt.where(WebSession.site_id == site_id)
    sessions = (await session.scalars(stmt)).all()
    page_views = await session.scalar(select(func.count()).select_from(WebEvent).where(
        WebEvent.organization_id == org, WebEvent.type == "page_view", WebEvent.created_at >= lo,
        WebEvent.created_at < hi)) or 0

    series = {d.isoformat(): {"sessions": 0, "clicks": 0, "conversations": 0} for d in _days(d0, d1)}
    groups: dict[tuple, Counter] = defaultdict(Counter)
    landings: Counter = Counter()
    for s in sessions:
        day = s.created_at.astimezone(tz).date().isoformat()
        clicked, matched = s.wa_click_at is not None, s.matched_conversation_id is not None
        if day in series:
            series[day]["sessions"] += 1
            series[day]["clicks"] += clicked
            series[day]["conversations"] += matched
        key = (_source(s), _medium(s), s.utm_campaign or "(sin campaña)")
        groups[key]["sessions"] += 1
        groups[key]["clicks"] += clicked
        groups[key]["conversations"] += matched
        if s.landing_url:
            landings[urlparse(s.landing_url).path or "/"] += 1
    total = len(sessions)
    clicks = sum(1 for s in sessions if s.wa_click_at)
    convs = sum(1 for s in sessions if s.matched_conversation_id)
    return {
        "totals": {"sessions": total, "page_views": page_views, "wa_clicks": clicks, "conversations": convs,
                   "click_rate_pct": _pct(clicks, total), "conversation_rate_pct": _pct(convs, clicks),
                   "paid_sessions": sum(1 for s in sessions if s.gclid or s.gbraid or s.wbraid or s.fbclid)},
        "series": [{"day": k, **v} for k, v in series.items()],
        "by_source": sorted([{"source": k[0], "medium": k[1], "campaign": k[2], **dict(v),
                              "click_rate_pct": _pct(v["clicks"], v["sessions"])} for k, v in groups.items()],
                            key=lambda r: -r["sessions"])[:50],
        "top_landings": [{"path": p, "sessions": n} for p, n in landings.most_common(15)],
    }


async def _attribution_rows(session: AsyncSession, org: int, lo, hi, channel: str | None = None):
    stmt = (select(Attribution, Conversation.status, Typification.is_success)
            .join(Conversation, Conversation.id == Attribution.conversation_id)
            .outerjoin(Typification, Typification.id == Conversation.typification_id)
            .where(Attribution.organization_id == org, Attribution.created_at >= lo, Attribution.created_at < hi))
    if channel:
        stmt = stmt.where(Attribution.channel == channel)
    return (await session.execute(stmt)).all()


async def _conversions_by_conv(session: AsyncSession, org: int, conv_ids: list[int]) -> dict[int, dict]:
    if not conv_ids:
        return {}
    rows = (await session.execute(
        select(ConversionEvent.conversation_id, func.count(), func.coalesce(func.sum(ConversionEvent.value), 0))
        .where(ConversionEvent.organization_id == org, ConversionEvent.conversation_id.in_(conv_ids))
        .group_by(ConversionEvent.conversation_id))).all()
    return {cid: {"count": n, "value": float(v)} for cid, n, v in rows}


@router.get("/reports/attribution", dependencies=FEATURE)
async def attribution_report(start: str | None = None, end: str | None = None, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, d0, d1 = await _range(session, org, date.fromisoformat(start) if start else None,
                                      date.fromisoformat(end) if end else None)
    rows = await _attribution_rows(session, org, lo, hi)
    conv = await _conversions_by_conv(session, org, [a.conversation_id for a, _s, _w in rows])
    by_channel: dict[str, Counter] = defaultdict(Counter)
    by_campaign: dict[tuple, Counter] = defaultdict(Counter)
    for a, status, success in rows:
        c = conv.get(a.conversation_id, {"count": 0, "value": 0.0})
        for bucket in (by_channel[a.channel], by_campaign[(a.channel, a.utm_campaign or a.ad_id or "(sin campaña)")]):
            bucket["conversations"] += 1
            bucket["sales"] += bool(success)
            bucket["closed"] += status == "closed"
            bucket["conversions"] += c["count"]
            bucket["value"] += c["value"]
    uploads = dict((await session.execute(
        select(ConversionUpload.status, func.count()).join(ConversionEvent, ConversionEvent.id == ConversionUpload.event_id)
        .where(ConversionEvent.organization_id == org, ConversionEvent.occurred_at >= lo, ConversionEvent.occurred_at < hi)
        .group_by(ConversionUpload.status))).all())
    total = len(rows)
    return {
        "totals": {"conversations": total, "sales": sum(v["sales"] for v in by_channel.values()),
                   "conversions": sum(v["conversions"] for v in by_channel.values()),
                   "value": sum(v["value"] for v in by_channel.values()),
                   "attributed_pct": _pct(sum(1 for a, _s, _w in rows if a.channel != "direct"), total)},
        "by_channel": sorted([{"channel": k, "label": CHANNEL_LABELS.get(k, k), **dict(v),
                               "sale_rate_pct": _pct(v["sales"], v["conversations"])} for k, v in by_channel.items()],
                             key=lambda r: -r["conversations"]),
        "by_campaign": sorted([{"channel": k[0], "campaign": k[1], **dict(v),
                                "sale_rate_pct": _pct(v["sales"], v["conversations"])} for k, v in by_campaign.items()],
                              key=lambda r: -r["conversations"])[:50],
        "uploads": {s: uploads.get(s, 0) for s in ("pending", "sent", "failed", "skipped")},
    }


@router.get("/reports/click-to-wa-google", dependencies=FEATURE)
async def click_to_wa_google(start: str | None = None, end: str | None = None, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    lo, hi, tz, d0, d1 = await _range(session, org, date.fromisoformat(start) if start else None,
                                      date.fromisoformat(end) if end else None)
    rows = await _attribution_rows(session, org, lo, hi, "google_ads")
    conv = await _conversions_by_conv(session, org, [a.conversation_id for a, _s, _w in rows])
    clicks = await session.scalar(select(func.count()).select_from(WebSession).where(
        WebSession.organization_id == org, WebSession.created_at >= lo, WebSession.created_at < hi,
        WebSession.wa_click_at.is_not(None),
        (WebSession.gclid.is_not(None)) | (WebSession.gbraid.is_not(None)) | (WebSession.wbraid.is_not(None)))) or 0
    by_campaign: dict[str, Counter] = defaultdict(Counter)
    by_keyword: dict[str, Counter] = defaultdict(Counter)
    series = {d.isoformat(): {"conversations": 0, "sales": 0} for d in _days(d0, d1)}
    for a, _status, success in rows:
        c = conv.get(a.conversation_id, {"count": 0, "value": 0.0})
        for bucket in (by_campaign[a.utm_campaign or "(sin campaña)"], by_keyword[a.utm_term or "(sin palabra clave)"]):
            bucket["conversations"] += 1
            bucket["sales"] += bool(success)
            bucket["conversions"] += c["count"]
            bucket["value"] += c["value"]
        day = a.created_at.astimezone(tz).date().isoformat()
        if day in series:
            series[day]["conversations"] += 1
            series[day]["sales"] += bool(success)
    uploads = dict((await session.execute(
        select(ConversionUpload.status, func.count()).join(ConversionEvent, ConversionEvent.id == ConversionUpload.event_id)
        .where(ConversionEvent.organization_id == org, ConversionUpload.destination == "google_ads",
               ConversionEvent.occurred_at >= lo, ConversionEvent.occurred_at < hi)
        .group_by(ConversionUpload.status))).all())
    total = len(rows)
    sales = sum(1 for _a, _s, w in rows if w)
    return {
        "totals": {"wa_clicks": clicks, "conversations": total, "sales": sales, "sale_rate_pct": _pct(sales, total),
                   "value": sum(v["value"] for v in by_campaign.values()),
                   "uploads_sent": uploads.get("sent", 0), "uploads_failed": uploads.get("failed", 0),
                   "uploads_pending": uploads.get("pending", 0)},
        "series": [{"day": k, **v} for k, v in series.items()],
        "by_campaign": sorted([{"campaign": k, **dict(v)} for k, v in by_campaign.items()],
                              key=lambda r: -r["conversations"]),
        "by_keyword": sorted([{"keyword": k, **dict(v)} for k, v in by_keyword.items()],
                             key=lambda r: -r["conversations"])[:30],
    }


@router.get("/attributions/conversation/{conv_id}", dependencies=FEATURE)
async def conversation_attribution(conv_id: int, agent: Agent = Depends(current_agent),
                                   session: AsyncSession = Depends(get_session)):
    a = await session.scalar(select(Attribution).where(Attribution.conversation_id == conv_id,
                                                       Attribution.organization_id == agent.organization_id))
    if not a:
        return None
    return {"channel": a.channel, "label": CHANNEL_LABELS.get(a.channel, a.channel), "matched_by": a.matched_by,
            "utm_source": a.utm_source, "utm_medium": a.utm_medium, "utm_campaign": a.utm_campaign,
            "utm_term": a.utm_term, "gclid": a.gclid, "ctwa_clid": a.ctwa_clid, "ad_id": a.ad_id,
            "landing_url": a.landing_url, "created_at": a.created_at}

