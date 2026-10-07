"""Botón flotante de WhatsApp embebible (docs/data-model.md §19.1).

Panel: /api/wa-widgets (CRUD, código para pegar). Público: /b/{key}.js (script sin dependencias) y
POST /b/{key}/event (impresiones y clics; límite por IP). Cada asesor del botón abre WhatsApp con el enlace corto de
su mensaje disparador (/t/l/{slug}: se atribuye la visita) o, si no tiene, con wa.me y el texto prellenado.
"""

import json
import re
import secrets
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.config import get_settings
from app.db import get_session
from app.models import Agent, Channel, WaLink, WaWidget

router = APIRouter(tags=["wa-widgets"])
env = get_settings()
DAYS = range(7)  # 0 = lunes
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
MAX_AGENTS = 10


class WidgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    config: dict = {}
    link_id: int | None = None
    is_active: bool = True


def _str(v, n: int) -> str | None:
    return str(v).strip()[:n] or None if v is not None else None


def _url(v) -> str | None:
    s = _str(v, 500)
    return s if s and s.startswith("https://") else None


async def clean_config(session: AsyncSession, org: int, raw: dict) -> dict:
    """Valida y normaliza la configuración (lo que no cumple se descarta o da 422)."""
    mode = raw.get("mode") if raw.get("mode") in ("single", "multi") else "single"
    agents = []
    for a in (raw.get("agents") or [])[:MAX_AGENTS]:
        if not isinstance(a, dict) or not _str(a.get("name"), 80):
            continue
        item = {"name": _str(a.get("name"), 80), "role": _str(a.get("role"), 80), "photo_url": _url(a.get("photo_url")),
                "prefill": _str(a.get("prefill"), 500), "channel_id": None, "link_id": None, "hours": None}
        for key, model in (("channel_id", Channel), ("link_id", WaLink)):
            if a.get(key) not in (None, ""):
                try:
                    row = await session.get(model, int(a[key]))
                except (TypeError, ValueError):
                    row = None
                if not row or row.organization_id != org:
                    raise HTTPException(422, f"{'Número' if key == 'channel_id' else 'Mensaje disparador'} inválido")
                item[key] = row.id
        h = a.get("hours")
        if isinstance(h, dict) and TIME_RE.match(str(h.get("from", ""))) and TIME_RE.match(str(h.get("to", ""))):
            days = sorted({int(d) for d in h.get("days") or list(DAYS) if str(d).isdigit() and int(d) in DAYS})
            item["hours"] = {"days": days, "from": h["from"], "to": h["to"]}
        agents.append(item)
    if not agents:
        raise HTTPException(422, "Agrega al menos un asesor o número")
    if mode == "single":
        agents = agents[:1]
    b = raw.get("button") if isinstance(raw.get("button"), dict) else {}
    show = raw.get("show_on") if isinstance(raw.get("show_on"), dict) else {}
    domains = sorted({str(d).strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
                      for d in raw.get("allowed_domains") or [] if str(d).strip()})
    return {
        "mode": mode, "agents": agents,
        "button": {"text": _str(b.get("text"), 40), "color": b.get("color") if COLOR_RE.match(str(b.get("color", ""))) else "#25d366",
                   "position": "left" if b.get("position") == "left" else "right"},
        "title": _str(raw.get("title"), 80) or "¿Hablamos por WhatsApp?",
        "greeting": _str(raw.get("greeting"), 300),
        "show_on": {"paths_include": [str(p)[:200] for p in show.get("paths_include") or [] if str(p).strip()][:20],
                    "paths_exclude": [str(p)[:200] for p in show.get("paths_exclude") or [] if str(p).strip()][:20],
                    "delay_s": max(0, min(int(show.get("delay_s") or 0), 600)),
                    "mobile": show.get("mobile", True) is not False, "desktop": show.get("desktop", True) is not False},
        "allowed_domains": domains,
        "timezone": _str(raw.get("timezone"), 64) or "America/Bogota",
    }


def _out(w: WaWidget) -> dict:
    return {"id": w.id, "name": w.name, "key": w.key, "config": w.config, "link_id": w.link_id,
            "is_active": w.is_active, "impressions": w.impressions, "clicks": w.clicks,
            "ctr_pct": round(100 * w.clicks / w.impressions, 1) if w.impressions else None,
            "embed": embed(w), "script_url": script_url(w), "created_at": w.created_at, "updated_at": w.updated_at}


def script_url(w: WaWidget) -> str:
    return f"{env.public_base_url.rstrip('/')}/b/{w.key}.js"


def embed(w: WaWidget) -> str:
    return f'<script src="{script_url(w)}" async></script>'


async def _get(session: AsyncSession, org: int, wid: int) -> WaWidget:
    w = await session.get(WaWidget, wid)
    if not w or w.organization_id != org:
        raise HTTPException(404, "Botón no encontrado")
    return w


async def _check_link(session: AsyncSession, org: int, link_id: int | None) -> int | None:
    if link_id is None:
        return None
    link = await session.get(WaLink, link_id)
    if not link or link.organization_id != org:
        raise HTTPException(422, "Mensaje disparador inválido")
    return link.id


@router.get("/api/wa-widgets")
async def list_widgets(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(WaWidget).where(WaWidget.organization_id == agent.organization_id)
                                  .order_by(WaWidget.id))).all()
    return [_out(w) for w in rows]


@router.post("/api/wa-widgets")
async def create_widget(body: WidgetIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    w = WaWidget(organization_id=org, name=body.name.strip(), key=secrets.token_urlsafe(12),
                 config=await clean_config(session, org, body.config), link_id=await _check_link(session, org, body.link_id),
                 is_active=body.is_active, created_by=agent.id)
    session.add(w)
    await session.commit()
    return _out(w)


@router.put("/api/wa-widgets/{wid}")
async def update_widget(wid: int, body: WidgetIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    w = await _get(session, org, wid)
    w.name, w.config = body.name.strip(), await clean_config(session, org, body.config)
    w.link_id, w.is_active = await _check_link(session, org, body.link_id), body.is_active
    await session.commit()
    return _out(w)


@router.delete("/api/wa-widgets/{wid}")
async def delete_widget(wid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    w = await _get(session, agent.organization_id, wid)
    await session.delete(w)
    await session.commit()
    return {"ok": True}


@router.get("/api/wa-widgets/{wid}/preview")
async def preview_widget(wid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """La configuración pública tal como la recibe el script (para la vista previa del panel)."""
    w = await _get(session, agent.organization_id, wid)
    return await public_config(session, w)


# --- Público -------------------------------------------------------------------------------------------------
async def public_config(session: AsyncSession, w: WaWidget) -> dict:
    cfg = w.config or {}
    base = env.public_base_url.rstrip("/")
    agents = []
    for a in cfg.get("agents") or []:
        href = None
        link_id = a.get("link_id") or w.link_id
        if link_id:
            link = await session.get(WaLink, link_id)
            if link and link.is_active:
                href = f"{base}/t/l/{link.slug}"
        if href is None:
            channel = await session.get(Channel, a["channel_id"]) if a.get("channel_id") else (await session.scalars(
                select(Channel).where(Channel.organization_id == w.organization_id, Channel.provider == "whatsapp_cloud")
                .order_by(Channel.id).limit(1))).first()
            phone = re.sub(r"\D", "", (channel.display_phone if channel else "") or "")
            prefill = a.get("prefill") or ""
            href = f"https://wa.me/{phone}" + (f"?text={quote(prefill)}" if prefill else "")
        agents.append({"name": a.get("name"), "role": a.get("role"), "photo_url": a.get("photo_url"), "href": href,
                       "hours": a.get("hours")})
    return {"mode": cfg.get("mode", "single"), "agents": agents, "button": cfg.get("button") or {},
            "title": cfg.get("title"), "greeting": cfg.get("greeting"), "show_on": cfg.get("show_on") or {},
            "timezone": cfg.get("timezone") or "America/Bogota"}


def _host(url: str | None) -> str | None:
    try:
        return (urlparse(url).hostname or "").lower() or None if url else None
    except ValueError:
        return None


def domain_allowed(w: WaWidget, origin: str | None) -> bool:
    domains = (w.config or {}).get("allowed_domains") or []
    if not domains:
        return True
    host = _host(origin)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


async def _public_widget(session: AsyncSession, key: str) -> WaWidget:
    w = await session.scalar(select(WaWidget).where(WaWidget.key == key, WaWidget.is_active))
    if not w:
        raise HTTPException(404, "Botón no encontrado")
    return w


@router.get("/b/{key}.js")
async def widget_script(key: str, request: Request, session: AsyncSession = Depends(get_session)):
    w = await _public_widget(session, key)
    cfg = await public_config(session, w)
    base = env.public_base_url.rstrip("/")
    body = (BUTTON_JS.replace("__BASE__", base).replace("__KEY__", key)
            .replace("__CFG__", json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__DOMAINS__", json.dumps((w.config or {}).get("allowed_domains") or [])))
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@router.post("/b/{key}/event")
async def widget_event(key: str, request: Request, session: AsyncSession = Depends(get_session)):
    """impression | click. Cuerpo text/plain JSON (sin preflight). Límite por IP."""
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
    n = await session.scalar(text("select public.rate_limit_hit(:b, 60)"), {"b": f"b:{key}:{ip}"})
    await session.commit()
    if n and n > 60:
        raise HTTPException(429, "Demasiadas solicitudes")
    w = await _public_widget(session, key)
    if not domain_allowed(w, request.headers.get("origin") or request.headers.get("referer")):
        raise HTTPException(403, "Dominio no permitido")
    try:
        data = json.loads((await request.body()) or b"{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    kind = data.get("type") if isinstance(data, dict) else None
    if kind == "impression":
        await session.execute(update(WaWidget).where(WaWidget.id == w.id).values(impressions=WaWidget.impressions + 1))
    elif kind == "click":
        await session.execute(update(WaWidget).where(WaWidget.id == w.id).values(clicks=WaWidget.clicks + 1))
    else:
        raise HTTPException(422, "Evento inválido")
    await session.commit()
    resp = JSONResponse({"ok": True})
    origin = request.headers.get("origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp


# Script del botón: sin dependencias, accesible (botón con aria-label, diálogo con foco), respeta reglas de página,
# horario por asesor (zona horaria del botón) y dispositivo. Se mantiene compacto a propósito.
BUTTON_JS = r"""(function(w,d){"use strict";
var B="__BASE__",K="__KEY__",C=__CFG__,DOM=__DOMAINS__;if(w.waButton&&w.waButton.k===K)return;
function okHost(){if(!DOM.length)return true;var h=w.location.hostname.toLowerCase();for(var i=0;i<DOM.length;i++){if(h===DOM[i]||h.slice(-DOM[i].length-1)==="."+DOM[i])return true}return false}
function match(p,pat){if(pat.indexOf("*")<0)return p===pat||p.indexOf(pat.replace(/\/$/,"")+"/")===0;return new RegExp("^"+pat.split("*").map(function(x){return x.replace(/[.+?^${}()|[\]\\]/g,"\\$&")}).join(".*")+"$").test(p)}
function okPath(){var s=C.show_on||{},p=w.location.pathname,inc=s.paths_include||[],exc=s.paths_exclude||[];
for(var i=0;i<exc.length;i++)if(match(p,exc[i]))return false;if(!inc.length)return true;for(var j=0;j<inc.length;j++)if(match(p,inc[j]))return true;return false}
function okDevice(){var s=C.show_on||{},m=w.matchMedia&&w.matchMedia("(max-width: 768px)").matches;return m?s.mobile!==false:s.desktop!==false}
function nowParts(){try{var f=new Intl.DateTimeFormat("en-GB",{timeZone:C.timezone||"America/Bogota",weekday:"short",hour:"2-digit",minute:"2-digit",hour12:false}).formatToParts(new Date()),o={};f.forEach(function(x){o[x.type]=x.value});
return{day:["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].indexOf(o.weekday),hm:o.hour+":"+o.minute}}catch(e){var n=new Date();return{day:(n.getDay()+6)%7,hm:("0"+n.getHours()).slice(-2)+":"+("0"+n.getMinutes()).slice(-2)}}}
function open(h){if(!h)return true;var n=nowParts();return h.days.indexOf(n.day)>=0&&n.hm>=h.from&&n.hm<h.to}
function ev(t){try{var x=new XMLHttpRequest();x.open("POST",B+"/b/"+K+"/event",true);x.setRequestHeader("Content-Type","text/plain");x.send(JSON.stringify({type:t}))}catch(e){}}
if(!okHost()||!okPath()||!okDevice())return;
var btn=C.button||{},col=btn.color||"#25d366",side=btn.position==="left"?"left":"right";
var css=".wab-b{position:fixed;bottom:20px;"+side+":20px;display:flex;align-items:center;gap:8px;border:0;border-radius:28px;background:"+col+";color:#fff;font:600 15px system-ui,sans-serif;padding:0 18px 0 14px;height:56px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000}"+
".wab-b svg{width:28px;height:28px}.wab-b.only{width:56px;padding:0;justify-content:center}.wab-b:focus-visible,.wab-a:focus-visible{outline:3px solid #111;outline-offset:2px}"+
".wab-p{position:fixed;bottom:88px;"+side+":20px;width:320px;max-width:calc(100vw - 32px);background:#fff;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);overflow:hidden;z-index:2147483000;font:14px/1.4 system-ui,sans-serif;color:#1f2937;display:none}"+
".wab-h{background:"+col+";color:#fff;padding:14px}.wab-h b{display:block;font-size:15px}.wab-l{padding:8px}"+
".wab-a{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;text-decoration:none;color:inherit}.wab-a:hover{background:#f3f4f6}"+
".wab-a img,.wab-i{width:42px;height:42px;border-radius:50%;object-fit:cover;background:#e5e7eb;display:flex;align-items:center;justify-content:center;font-weight:700;color:#374151}"+
".wab-a small{display:block;color:#6b7280}.wab-a.off{opacity:.55;pointer-events:none}";
var ico='<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2a8.2 8.2 0 0 1-4.2-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8s-.4-.1-.6.1-.7.8-.8 1-.3.2-.5.1a6.7 6.7 0 0 1-3.3-2.9c-.2-.4.2-.4.7-1.3a.5.5 0 0 0 0-.4c0-.1-.6-1.4-.8-1.9s-.4-.5-.6-.5h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2 5.2 5.2 0 0 0 1.1 2.7 11.8 11.8 0 0 0 4.5 4c1.7.7 2.4.8 3.2.6a2.8 2.8 0 0 0 1.8-1.3 2.3 2.3 0 0 0 .2-1.3c-.1-.1-.3-.2-.5-.3Z"/></svg>';
function el(t,c,h){var e=d.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e}
function txt(e,v){e.textContent=v==null?"":String(v);return e}
function mount(){var s=el("style");s.textContent=css;d.head.appendChild(s);var A=C.agents||[];
var b=el("button","wab-b"+(btn.text?"":" only"),ico);b.type="button";b.setAttribute("aria-label",btn.text||"Abrir WhatsApp");if(btn.text)b.appendChild(txt(el("span"),btn.text));
d.body.appendChild(b);ev("impression");
if(C.mode!=="multi"||A.length<2){b.onclick=function(){ev("click");var a=A[0];if(a)w.open(a.href,"_blank","noopener")};return}
var p=el("div","wab-p");p.setAttribute("role","dialog");p.setAttribute("aria-label",C.title||"WhatsApp");var h=el("div","wab-h");h.appendChild(txt(el("b"),C.title||""));if(C.greeting)h.appendChild(txt(el("span"),C.greeting));p.appendChild(h);
var l=el("div","wab-l");A.forEach(function(a){var on=open(a.hours),x=el("a","wab-a"+(on?"":" off"));x.href=a.href;x.target="_blank";x.rel="noopener";
if(a.photo_url){var im=el("img");im.src=a.photo_url;im.alt="";x.appendChild(im)}else x.appendChild(txt(el("span","wab-i"),(a.name||"?").charAt(0)));
var t=el("span");t.appendChild(txt(el("span"),a.name));t.appendChild(txt(el("small"),on?(a.role||""):"Fuera de horario"));x.appendChild(t);
x.onclick=function(){ev("click")};l.appendChild(x)});p.appendChild(l);d.body.appendChild(p);
b.setAttribute("aria-expanded","false");b.onclick=function(){var o=p.style.display!=="block";p.style.display=o?"block":"none";b.setAttribute("aria-expanded",o?"true":"false");if(o){var f=p.querySelector("a:not(.off)");f&&f.focus()}};
d.addEventListener("keydown",function(e){if(e.key==="Escape"&&p.style.display==="block"){p.style.display="none";b.focus()}})}
var delay=((C.show_on||{}).delay_s||0)*1000;function go(){d.body?mount():d.addEventListener("DOMContentLoaded",mount)}delay?setTimeout(go,delay):go();
w.waButton={k:K};
})(window,document);"""
