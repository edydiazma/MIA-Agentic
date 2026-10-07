"""Tracking web público (sin autenticación): script, sesiones, eventos y redirección a WhatsApp.

Flujo: el sitio carga /t/{key}.js → POST /t/collect crea la sesión y devuelve un ref_code → los enlaces de
WhatsApp pasan por /t/wa/{key}?r=CODE, que registra el clic y redirige a wa.me con "(ref: CODE)" en el texto.
Cuando el cliente escribe, app.attribution encuentra el código y une la conversación con la visita.

Mensajes disparadores (wa_links): /t/l/{slug} registra el clic con los parámetros de la URL (UTMs, gclid,
fbclid…), crea la visita con link_id y redirige a wa.me con el texto del enlace y "(ref: CODE)". Si la página
ya tiene el script, este agrega ?r=CODE a esos enlaces y se reutiliza la visita en lugar de crear otra.
"""

import json
import re
from datetime import timedelta
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attribution import REF_WINDOW, hash_ip, new_ref_code
from app.config import get_settings
from app.db import get_session
from app.models import Channel, TrackingSite, WaLink, WebEvent, WebSession, utcnow

router = APIRouter(prefix="/t", tags=["tracking"])
MAX_FIELD = 1000
PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "gbraid", "wbraid",
          "fbclid", "ttclid", "msclkid")


async def _site(session: AsyncSession, key: str) -> TrackingSite:
    site = await session.scalar(select(TrackingSite).where(TrackingSite.public_key == key, TrackingSite.is_active))
    if not site:
        raise HTTPException(404, "Sitio no encontrado")
    return site


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None


def origin_allowed(site: TrackingSite, origin: str | None) -> bool:
    domains = [d.strip().lower().lstrip(".") for d in site.allowed_domains or [] if d.strip()]
    if not domains:
        return True
    host = _host(origin)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _cors(request: Request, resp: Response) -> Response:
    origin = request.headers.get("origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "false"
    return resp


async def _body(request: Request) -> dict:
    """El script envía text/plain (petición simple: sin preflight CORS)."""
    try:
        data = json.loads((await request.body()) or b"{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON inválido") from None


def _clip(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:MAX_FIELD] or None


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


async def _unique_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = new_ref_code()
        if not await session.scalar(select(WebSession.id).where(
                WebSession.ref_code == code, WebSession.created_at >= utcnow() - REF_WINDOW)):
            return code
    raise HTTPException(503, "No se pudo generar un código")


def script(site: TrackingSite) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return SCRIPT.replace("__BASE__", base).replace("__KEY__", site.public_key)


# Script del sitio (sin dependencias). Se mantiene pequeño a propósito.
SCRIPT = r"""(function(w,d){"use strict";
var B="__BASE__",K="__KEY__",S="_wa_ref_"+K,V="_wa_vid";
if(w.waAgent&&w.waAgent.k===K)return;
function ck(n){var m=d.cookie.match(new RegExp("(?:^|; )"+n+"=([^;]*)"));return m?decodeURIComponent(m[1]):null}
function setck(n,v,days){d.cookie=n+"="+encodeURIComponent(v)+"; Max-Age="+(days*86400)+"; Path=/; SameSite=Lax"}
var vid=ck(V);if(!vid){vid=(Date.now().toString(36)+Math.random().toString(36).slice(2,10));}setck(V,vid,365);
var q=new URLSearchParams(w.location.search),P={};
["utm_source","utm_medium","utm_campaign","utm_term","utm_content","gclid","gbraid","wbraid","fbclid","ttclid","msclkid"].forEach(function(k){var v=q.get(k);if(v)P[k]=v});
var ga=ck("_ga");if(ga){P.ga_client_id=ga.split(".").slice(-2).join(".")}
var fbc=ck("_fbc"),fbp=ck("_fbp");if(fbc)P.fbc=fbc;if(fbp)P.fbp=fbp;
function post(path,data,cb){try{var x=new XMLHttpRequest();x.open("POST",B+path,true);x.setRequestHeader("Content-Type","text/plain");
x.onload=function(){if(cb&&x.status<300){try{cb(JSON.parse(x.responseText))}catch(e){}}};x.send(JSON.stringify(data))}catch(e){}}
function store(){try{return JSON.parse(sessionStorage.getItem(S)||"null")}catch(e){return null}}
function rewrite(ref){var a=d.querySelectorAll('a[href*="wa.me"],a[href*="api.whatsapp.com"]');
for(var i=0;i<a.length;i++){if(a[i].getAttribute("data-wa-agent"))continue;a[i].setAttribute("data-wa-agent","1");a[i].href=B+"/t/wa/"+K+"?r="+ref}
var l=d.querySelectorAll('a[href^="'+B+'/t/l/"]');for(var j=0;j<l.length;j++){if(l[j].getAttribute("data-wa-agent"))continue;
l[j].setAttribute("data-wa-agent","1");var u=new URL(l[j].href);u.searchParams.set("r",ref);l[j].href=u.toString()}}
function ready(ref){w.waAgent.ref=ref;rewrite(ref);if(w.MutationObserver){new MutationObserver(function(){rewrite(ref)}).observe(d.documentElement,{childList:true,subtree:true})}}
w.waAgent={k:K,ref:null,open:function(){w.location.href=B+"/t/wa/"+K+(w.waAgent.ref?"?r="+w.waAgent.ref:"")},
track:function(name){if(w.waAgent.ref)post("/t/event",{k:K,r:w.waAgent.ref,type:"custom",name:String(name||""),url:w.location.href})}};
var s=store();
post("/t/collect",{k:K,vid:vid,r:s&&s.r,url:w.location.href,referrer:d.referrer||null,params:P},function(res){
if(!res||!res.ref_code)return;try{sessionStorage.setItem(S,JSON.stringify({r:res.ref_code}))}catch(e){}
if(d.readyState==="loading"){d.addEventListener("DOMContentLoaded",function(){ready(res.ref_code)})}else{ready(res.ref_code)}});
})(window,document);"""


@router.get("/{public_key}.js")
async def tracking_script(public_key: str, session: AsyncSession = Depends(get_session)):
    site = await _site(session, public_key)
    return Response(script(site), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@router.post("/collect")
async def collect(request: Request, session: AsyncSession = Depends(get_session)):
    """Crea la sesión de la visita (o actualiza la existente) y devuelve su ref_code."""
    data = await _body(request)
    site = await _site(session, str(data.get("k", "")))
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin_allowed(site, origin or data.get("url")):
        raise HTTPException(403, "Dominio no permitido para este sitio")
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    vid = _clip(data.get("vid")) or "anon"
    url = _clip(data.get("url"))

    ws = None
    if data.get("r"):  # misma sesión del navegador: se reutiliza el código
        ws = (await session.scalars(select(WebSession).where(
            WebSession.site_id == site.id, WebSession.ref_code == str(data["r"]).upper(),
            WebSession.created_at >= utcnow() - timedelta(days=1)).limit(1))).first()
    if ws is None:
        ws = WebSession(organization_id=site.organization_id, site_id=site.id, visitor_id=vid,
                        ref_code=await _unique_code(session), landing_url=url, referrer=_clip(data.get("referrer")),
                        user_agent=_clip(request.headers.get("user-agent")), ip_hash=hash_ip(_client_ip(request)))
        for p in PARAMS:
            setattr(ws, p, _clip(params.get(p)))
        for p in ("fbc", "fbp", "ga_client_id"):
            setattr(ws, p, _clip(params.get(p)))
        session.add(ws)
        await session.flush()
    else:
        ws.last_seen_at = utcnow()
        for p in PARAMS:  # un clic pagado nuevo dentro de la misma sesión actualiza los identificadores
            if params.get(p):
                setattr(ws, p, _clip(params.get(p)))
    session.add(WebEvent(organization_id=site.organization_id, session_id=ws.id, type="page_view", url=url))
    await session.commit()
    return _cors(request, JSONResponse({"ref_code": ws.ref_code}))


@router.post("/event")
async def event(request: Request, session: AsyncSession = Depends(get_session)):
    data = await _body(request)
    site = await _site(session, str(data.get("k", "")))
    if not origin_allowed(site, request.headers.get("origin") or data.get("url")):
        raise HTTPException(403, "Dominio no permitido para este sitio")
    ws = (await session.scalars(select(WebSession).where(
        WebSession.site_id == site.id, WebSession.ref_code == str(data.get("r", "")).upper(),
        WebSession.created_at >= utcnow() - REF_WINDOW).limit(1))).first()
    if not ws:
        raise HTTPException(404, "Sesión no encontrada")
    etype = data.get("type") if data.get("type") in ("page_view", "custom") else "custom"
    session.add(WebEvent(organization_id=site.organization_id, session_id=ws.id, type=etype,
                         name=_clip(data.get("name")), url=_clip(data.get("url"))))
    ws.last_seen_at = utcnow()
    await session.commit()
    return _cors(request, JSONResponse({"ok": True}))


@router.get("/wa/{public_key}")
async def wa_redirect(public_key: str, r: str | None = None, session: AsyncSession = Depends(get_session)):
    """Registra el clic y redirige a WhatsApp con el código en el mensaje prellenado."""
    site = await _site(session, public_key)
    channel = await session.get(Channel, site.channel_id) if site.channel_id else (await session.scalars(
        select(Channel).where(Channel.organization_id == site.organization_id).order_by(Channel.id).limit(1))).first()
    phone = re.sub(r"\D", "", (channel.display_phone if channel else "") or "")
    text = site.wa_prefill
    if r:
        ws = (await session.scalars(select(WebSession).where(
            WebSession.site_id == site.id, WebSession.ref_code == r.upper(),
            WebSession.created_at >= utcnow() - REF_WINDOW).limit(1))).first()
        if ws:
            ws.wa_click_at = ws.wa_click_at or utcnow()
            ws.last_seen_at = utcnow()
            session.add(WebEvent(organization_id=site.organization_id, session_id=ws.id, type="wa_click",
                                 url=ws.landing_url))
            await session.commit()
            text = f"{text} (ref: {ws.ref_code})"
    target = f"https://wa.me/{phone}?text={quote(text)}" if phone else f"https://wa.me/?text={quote(text)}"
    return RedirectResponse(target, status_code=302)


def _wa_url(channel: Channel | None, text: str) -> str:
    phone = re.sub(r"\D", "", (channel.display_phone if channel else "") or "")
    return f"https://wa.me/{phone}?text={quote(text)}" if phone else f"https://wa.me/?text={quote(text)}"


async def link_channel(session: AsyncSession, link: WaLink) -> Channel | None:
    if link.channel_id:
        return await session.get(Channel, link.channel_id)
    return (await session.scalars(select(Channel).where(Channel.organization_id == link.organization_id)
                                  .order_by(Channel.id).limit(1))).first()


def direct_url(channel: Channel | None, link: WaLink) -> str:
    """wa.me sin redirección (anuncios Click to WhatsApp, impresos): se atribuye por el texto."""
    return _wa_url(channel, link.trigger_text)


@router.get("/l/{slug}")
async def link_redirect(slug: str, request: Request, r: str | None = None, session: AsyncSession = Depends(get_session)):
    """Enlace corto de un mensaje disparador: registra la visita y el clic y abre WhatsApp."""
    link = await session.scalar(select(WaLink).where(WaLink.slug == slug.lower()))
    if not link:
        raise HTTPException(404, "Enlace no encontrado")
    channel = await link_channel(session, link)
    if not link.is_active:  # enlace pausado: WhatsApp igual abre, sin tracking
        return RedirectResponse(direct_url(channel, link), status_code=302)
    q = request.query_params
    ws = None
    if r:  # la página tenía el script: misma visita
        ws = (await session.scalars(select(WebSession).where(
            WebSession.organization_id == link.organization_id, WebSession.ref_code == r.upper(),
            WebSession.created_at >= utcnow() - REF_WINDOW).limit(1))).first()
    if ws is None:
        ws = WebSession(organization_id=link.organization_id, link_id=link.id,
                        visitor_id=_clip(q.get("vid")) or "link", ref_code=await _unique_code(session),
                        landing_url=_clip(str(request.url)), referrer=_clip(request.headers.get("referer")),
                        user_agent=_clip(request.headers.get("user-agent")), ip_hash=hash_ip(_client_ip(request)))
        session.add(ws)
    ws.link_id = ws.link_id or link.id
    for p in PARAMS:  # los parámetros del enlace (anuncio) mandan sobre los de la visita
        if q.get(p):
            setattr(ws, p, _clip(q.get(p)))
    for f in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
        if not getattr(ws, f) and getattr(link, f):
            setattr(ws, f, getattr(link, f))
    ws.wa_click_at = ws.wa_click_at or utcnow()
    ws.last_seen_at = utcnow()
    await session.flush()
    session.add(WebEvent(organization_id=link.organization_id, session_id=ws.id, type="wa_click",
                         url=_clip(str(request.url))))
    await session.commit()
    text = f"{link.trigger_text} (ref: {ws.ref_code})" if link.append_ref else link.trigger_text
    return RedirectResponse(_wa_url(channel, text), status_code=302)
