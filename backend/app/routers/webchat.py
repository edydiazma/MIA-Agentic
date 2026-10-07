"""Chat web público (sin autenticación de panel): widget, sesión del visitante, mensajes y WebSocket.

El visitante se identifica con un token aleatorio (solo se guarda su sha256 en webchat_sessions); cada llamada
lleva la llave pública del canal (`key`) y el token. Un visitante solo ve los mensajes de SU conversación.
CORS: solo los dominios de settings.allowed_domains del canal (vacío = cualquiera). Límites por IP compartidos
entre réplicas con public.rate_limit_hit(). Las peticiones POST usan text/plain o multipart para evitar
preflight CORS. docs/data-model.md §12.2
"""

import asyncio
import hashlib
import json
import logging
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.attribution import hash_ip
from app.channels import webchat as live
from app.config import get_settings
from app.db import SessionLocal, get_session, set_actor
from app.models import Channel, Message, WebchatSession, utcnow
from app.service import get_or_create_contact_by_identity, get_or_create_conversation, record_message

router = APIRouter(prefix="/w", tags=["webchat"])
log = logging.getLogger(__name__)
MAX_TEXT = 4000
MAX_FILE = 10 * 1024 * 1024
LIMITS = {"session": (30, 60), "message": (40, 60), "read": (240, 60)}  # (golpes, ventana en s) por IP
POLL_S = 3.0


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _host(url: str | None) -> str | None:
    try:
        return (urlparse(url).hostname or "").lower() or None if url else None
    except ValueError:
        return None


def origin_allowed(channel: Channel, origin: str | None) -> bool:
    domains = [d.lstrip(".") for d in (channel.settings or {}).get("allowed_domains") or [] if d]
    if not domains:
        return True
    host = _host(origin)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _cors(request: Request, resp: Response) -> Response:
    origin = request.headers.get("origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _limit(session: AsyncSession, request: Request, kind: str) -> None:
    hits, window = LIMITS[kind]
    n = await session.scalar(text("select public.rate_limit_hit(:b, :w)"),
                             {"b": f"w:{kind}:{_ip(request)}", "w": window})
    await session.commit()
    if n and n > hits:
        raise HTTPException(429, "Demasiadas solicitudes; intenta en un momento")


async def _channel(session: AsyncSession, key: str) -> Channel:
    ch = await session.scalar(select(Channel).where(Channel.provider == "webchat", Channel.external_id == key,
                                                    Channel.status == "active"))
    if not ch:
        raise HTTPException(404, "Chat no encontrado")
    return ch


async def _body(request: Request) -> dict:
    try:
        data = json.loads((await request.body()) or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON inválido") from None
    return data if isinstance(data, dict) else {}


async def _visitor(session: AsyncSession, channel: Channel, token: str | None) -> WebchatSession:
    if not token:
        raise HTTPException(401, "Falta el token del visitante")
    ws = await session.scalar(select(WebchatSession).where(WebchatSession.channel_id == channel.id,
                                                           WebchatSession.token_hash == _hash(token)))
    if not ws:
        raise HTTPException(401, "Sesión no válida")
    return ws


def public_settings(channel: Channel) -> dict:
    s = channel.settings or {}
    return {k: s.get(k) for k in ("title", "greeting", "color", "position", "prechat", "open_selector", "auto_open_s",
                                  "initial_message", "clear_on_open", "avatar_url", "theme", "launcher_text",
                                  "header_text_color")}


def widget_message(m: Message, key: str, token: str) -> dict:
    sender = "visitor" if m.direction == "in" else ("agent" if m.sender_type == "agent" else "bot")
    meta = m.metadata_ or {}
    out = {"id": m.id, "sender": sender, "type": m.type, "text": m.text, "created_at": m.created_at.isoformat(),
           "buttons": meta.get("buttons") or meta.get("list") or []}
    if m.media_path:
        out["media_url"] = f"/w/media/{m.id}?key={key}&token={token}"
        out["media_mime"] = m.media_mime
    return out


async def _messages(session: AsyncSession, ws: WebchatSession, after: int = 0, limit: int = 100) -> list[Message]:
    if not ws.conversation_id:
        return []
    return list((await session.scalars(
        select(Message).where(Message.conversation_id == ws.conversation_id, Message.id > after,
                              Message.sender_type != "system").order_by(Message.id).limit(limit))).all())


# --- Widget ------------------------------------------------------------------------
@router.get("/{key}.js")
async def widget_script(key: str, session: AsyncSession = Depends(get_session)):
    channel = await _channel(session, key)
    base = get_settings().public_base_url.rstrip("/")
    initial = json.dumps(public_settings(channel), ensure_ascii=False).replace("</", "<\\/")
    body = WIDGET.replace("__BASE__", base).replace("__KEY__", key).replace("__SETTINGS__", initial)
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@router.post("/session")
async def start_session(request: Request, session: AsyncSession = Depends(get_session)):
    """Crea o retoma la sesión del visitante. Devuelve el token (el widget lo guarda en localStorage)."""
    await _limit(session, request, "session")
    data = await _body(request)
    channel = await _channel(session, str(data.get("key", "")))
    if not origin_allowed(channel, request.headers.get("origin") or data.get("page_url")):
        raise HTTPException(403, "Dominio no permitido para este chat")
    token = str(data.get("token") or "")
    ws = None
    if token:
        ws = await session.scalar(select(WebchatSession).where(WebchatSession.channel_id == channel.id,
                                                               WebchatSession.token_hash == _hash(token)))
    if ws is None:
        token = secrets.token_urlsafe(24)
        visitor = str(data.get("visitor_id") or "")[:64] or secrets.token_hex(8)
        ws = await session.scalar(select(WebchatSession).where(WebchatSession.channel_id == channel.id,
                                                               WebchatSession.visitor_id == visitor))
        if ws is None:  # mismo navegador (cookie de tracking) en otra pestaña: se reutiliza con token nuevo
            ws = WebchatSession(organization_id=channel.organization_id, channel_id=channel.id, visitor_id=visitor,
                                token_hash=_hash(token))
            session.add(ws)
        else:
            ws.token_hash = _hash(token)
    ws.page_url = str(data.get("page_url") or "")[:1000] or ws.page_url
    ws.user_agent = (request.headers.get("user-agent") or "")[:500] or None
    ws.ip_hash = hash_ip(_ip(request))
    if data.get("ref"):
        ws.web_session_ref = str(data["ref"]).upper()[:12]
    ws.last_seen_at = utcnow()
    await session.flush()
    name, email = str(data.get("name") or "").strip()[:120], str(data.get("email") or "").strip()[:200]
    if name or email:  # formulario previo
        await _prechat_store(session, ws, name, email)
    await session.commit()
    msgs = await _messages(session, ws)
    return _cors(request, JSONResponse({"token": token, "visitor_id": ws.visitor_id, "settings": public_settings(channel),
                                        "messages": [widget_message(m, channel.external_id, token) for m in msgs]}))


async def _prechat_store(session: AsyncSession, ws: WebchatSession, name: str, email: str) -> None:
    """Formulario previo: crea (o completa) el contacto para que el asesor vea nombre y correo."""
    channel = await session.get(Channel, ws.channel_id)
    contact, _ident, _new = await get_or_create_contact_by_identity(session, channel, ws.visitor_id, name=name or None)
    if email and not contact.email:
        contact.email = email
    ws.contact_id = contact.id


@router.post("/messages")
async def post_message(request: Request, session: AsyncSession = Depends(get_session)):
    """Mensaje del visitante: texto (JSON en text/plain) o archivo (multipart: key, token, file, text)."""
    await _limit(session, request, "message")
    upload = None
    if (request.headers.get("content-type") or "").startswith("multipart/"):
        form = await request.form()
        data = {k: form.get(k) for k in ("key", "token", "text")}
        upload = form.get("file")
    else:
        data = await _body(request)
    channel = await _channel(session, str(data.get("key") or ""))
    if not origin_allowed(channel, request.headers.get("origin")):
        raise HTTPException(403, "Dominio no permitido para este chat")
    token = str(data.get("token") or "")
    ws = await _visitor(session, channel, token)
    body = str(data.get("text") or "").strip()[:MAX_TEXT]
    file_bytes = await upload.read() if upload is not None and hasattr(upload, "read") else None
    if not body and not file_bytes:
        raise HTTPException(422, "Mensaje vacío")
    if file_bytes and len(file_bytes) > MAX_FILE:
        raise HTTPException(413, "Archivo demasiado grande (máx. 10 MB)")
    msg = await receive(session, channel, ws, body or None, file_bytes,
                        getattr(upload, "content_type", None), getattr(upload, "filename", None))
    return _cors(request, JSONResponse(widget_message(msg, channel.external_id, token)))


async def receive(session: AsyncSession, channel: Channel, ws: WebchatSession, body: str | None,
                  file_bytes: bytes | None = None, mime: str | None = None, filename: str | None = None) -> Message:
    """Guarda el mensaje del visitante y corre el mismo flujo que WhatsApp (atribución, flujos, IA)."""
    from app.agent import schedule_reply
    from app.ingest import after_inbound

    await set_actor(session, "contact")
    contact, ident, is_new = await get_or_create_contact_by_identity(session, channel, ws.visitor_id)
    if contact.blocked:
        raise HTTPException(403, "Chat no disponible")
    now = utcnow()
    ident.last_inbound_at, contact.last_seen_at, ws.last_seen_at = now, now, now
    ws.contact_id = contact.id
    conv = await get_or_create_conversation(session, channel, contact)
    first = ws.conversation_id != conv.id
    ws.conversation_id = conv.id
    msg = Message(direction="in", sender_type="contact", type="text", text=body, wa_message_id=live.new_id(),
                  status="received")
    if file_bytes:
        mime = (mime or "application/octet-stream").split(";")[0]
        msg.type = storage.kind_for_mime(mime)
        msg.media_path = await storage.upload(
            storage.new_path(storage.MEDIA_BUCKET, channel.organization_id, f"conv/{conv.id}", mime), file_bytes, mime)
        msg.media_mime, msg.media_size, msg.media_filename = mime, len(file_bytes), filename
    await record_message(session, conv, msg)
    raw = {"ref_code": ws.web_session_ref} if first and ws.web_session_ref else {}
    status, conv_id, handled = await after_inbound(session, conv, msg, raw, is_new, "webchat")
    if status == "bot" and not handled:
        schedule_reply(conv_id)
    return msg


@router.get("/messages")
async def get_messages(request: Request, key: str, token: str, after: int = 0,
                       session: AsyncSession = Depends(get_session)):
    """Respaldo sin WebSocket: mensajes nuevos de la conversación del visitante."""
    await _limit(session, request, "read")
    channel = await _channel(session, key)
    ws = await _visitor(session, channel, token)
    msgs = await _messages(session, ws, after)
    return _cors(request, JSONResponse([widget_message(m, key, token) for m in msgs]))


@router.get("/media/{message_id}")
async def media(message_id: int, key: str, token: str, session: AsyncSession = Depends(get_session)):
    channel = await _channel(session, key)
    ws = await _visitor(session, channel, token)
    m = await session.scalar(select(Message).where(Message.id == message_id))
    if not m or not m.media_path or m.conversation_id != ws.conversation_id:
        raise HTTPException(404, "Archivo no encontrado")
    return Response(await storage.download(m.media_path), media_type=m.media_mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.websocket("/ws")
async def visitor_socket(websocket: WebSocket, key: str, token: str, after: int = 0):
    """Entrega en vivo de las respuestas. Solo la conversación del visitante; aviso local + consulta periódica
    (funciona con varias réplicas del backend)."""
    async with SessionLocal() as session:
        try:
            channel = await _channel(session, key)
            ws = await _visitor(session, channel, token)
        except HTTPException:
            await websocket.close(code=4401)
            return
        if not origin_allowed(channel, websocket.headers.get("origin")):
            await websocket.close(code=4403)
            return
        session_id = ws.id
    await websocket.accept()
    last = after
    conv_id = None
    event = None
    try:
        while True:
            async with SessionLocal() as session:
                ws = await session.get(WebchatSession, session_id)
                if ws is None:
                    break
                if ws.conversation_id != conv_id:
                    if event is not None and conv_id is not None:
                        live.unsubscribe(conv_id, event)
                    conv_id = ws.conversation_id
                    event = live.subscribe(conv_id) if conv_id else None
                msgs = await _messages(session, ws, last)
            for m in msgs:
                await websocket.send_json({"type": "message", "message": widget_message(m, key, token)})
                last = max(last, m.id)
            if event is not None:
                try:
                    await asyncio.wait_for(event.wait(), POLL_S)
                except TimeoutError:
                    pass
                event.clear()
            else:
                await asyncio.sleep(POLL_S)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if event is not None and conv_id is not None:
            live.unsubscribe(conv_id, event)


# Widget sin dependencias. Se mantiene compacto a propósito.
WIDGET = r"""(function(w,d){"use strict";
var B="__BASE__",K="__KEY__",TK="_wa_chat_"+K;if(w.waChat&&w.waChat.k===K)return;
function ck(n){var m=d.cookie.match(new RegExp("(?:^|; )"+n+"=([^;]*)"));return m?decodeURIComponent(m[1]):null}
function ls(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
var st={token:ls(TK),last:0,open:false,sock:null,settings:__SETTINGS__||{},seen:{}};
var css=".wac-b{position:fixed;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;color:#fff;font-size:26px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000}"+
".wac-p{position:fixed;bottom:88px;width:340px;max-width:calc(100vw - 32px);height:480px;max-height:calc(100vh - 120px);background:#fff;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font:14px/1.4 system-ui,sans-serif;color:#1f2937}"+
".wac-h{padding:12px 14px;color:#fff;font-weight:600}.wac-m{flex:1;overflow:auto;padding:12px;background:#f3f4f6}"+
".wac-r{max-width:80%;margin:4px 0;padding:8px 10px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}"+
".wac-v{margin-left:auto;color:#fff}.wac-a{background:#fff}.wac-q{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}"+
".wac-q button{border:1px solid #d1d5db;background:#fff;border-radius:14px;padding:4px 10px;cursor:pointer}"+
".wac-f{display:flex;gap:6px;padding:8px;border-top:1px solid #e5e7eb}.wac-f input[type=text]{flex:1;border:1px solid #d1d5db;border-radius:8px;padding:8px}"+
".wac-f button,.wac-pre button{border:0;color:#fff;border-radius:8px;padding:0 12px;cursor:pointer}.wac-img{max-width:100%;border-radius:8px}"+
".wac-pre{padding:14px;display:flex;flex-direction:column;gap:8px}.wac-pre input{border:1px solid #d1d5db;border-radius:8px;padding:8px}.wac-pre button{padding:9px}"+
".wac-b.txt{width:auto;border-radius:28px;padding:0 18px;font:600 15px system-ui,sans-serif}.wac-h{display:flex;align-items:center;gap:10px}.wac-av{width:32px;height:32px;border-radius:50%;object-fit:cover;background:#fff}"+
".wac-dark .wac-p,.wac-p.wac-dark{background:#111827;color:#f9fafb}.wac-p.wac-dark .wac-m{background:#1f2937}.wac-p.wac-dark .wac-a{background:#374151;color:#f9fafb}"+
".wac-p.wac-dark .wac-f{border-top-color:#374151}.wac-p.wac-dark input{background:#1f2937;color:#f9fafb;border-color:#4b5563}";
var s=d.createElement("style");s.textContent=css;d.head.appendChild(s);
var btn=d.createElement("button"),pan=d.createElement("div");btn.className="wac-b";btn.setAttribute("aria-label","Abrir chat");btn.textContent="\u{1F4AC}";
pan.className="wac-p";pan.setAttribute("role","dialog");
pan.innerHTML='<div class="wac-h"></div><div class="wac-m" aria-live="polite"></div><form class="wac-f"><label style="cursor:pointer;align-self:center" title="Adjuntar">\u{1F4CE}<input type="file" accept="image/*,application/pdf" hidden></label><input type="text" placeholder="Escribe un mensaje" aria-label="Mensaje"><button type="submit">Enviar</button></form>';
d.body.appendChild(btn);d.body.appendChild(pan);
var H=pan.querySelector(".wac-h"),M=pan.querySelector(".wac-m"),F=pan.querySelector(".wac-f"),I=F.querySelector("input[type=text]"),FI=F.querySelector("input[type=file]");
function color(){return st.settings.color||"#0f766e"}
function dark(){var t=st.settings.theme;return t==="dark"||(t==="auto"&&w.matchMedia&&w.matchMedia("(prefers-color-scheme: dark)").matches)}
function paint(){var c=color(),S=st.settings;btn.style.background=c;H.style.background=c;H.style.color=S.header_text_color||"#fff";F.querySelector("button").style.background=c;
var side=S.position==="left"?"left":"right";btn.style[side]="20px";pan.style[side]="20px";H.textContent="";
if(S.avatar_url){var av=d.createElement("img");av.className="wac-av";av.src=S.avatar_url;av.alt="";H.appendChild(av)}
var ti=d.createElement("span");ti.textContent=S.title||"Chat";H.appendChild(ti);
if(S.launcher_text){btn.className="wac-b txt";btn.textContent="\u{1F4AC} "+S.launcher_text}pan.classList.toggle("wac-dark",dark());}
function add(m){if(st.seen[m.id])return;st.seen[m.id]=1;st.last=Math.max(st.last,m.id);var r=d.createElement("div");
r.className="wac-r "+(m.sender==="visitor"?"wac-v":"wac-a");if(m.sender==="visitor")r.style.background=color();
if(m.media_url&&(m.media_mime||"").indexOf("image/")===0){var im=d.createElement("img");im.className="wac-img";im.src=B+m.media_url;r.appendChild(im)}
else if(m.media_url){var a=d.createElement("a");a.href=B+m.media_url;a.target="_blank";a.textContent="\u{1F4C4} Archivo";r.appendChild(a)}
if(m.text){var t=d.createElement("div");t.textContent=m.text;r.appendChild(t)}M.appendChild(r);
if(m.sender!=="visitor"&&m.buttons&&m.buttons.length){var q=d.createElement("div");q.className="wac-q";m.buttons.forEach(function(b){var x=d.createElement("button");x.type="button";x.textContent=b;x.onclick=function(){q.remove();send(b)};q.appendChild(x)});M.appendChild(q)}
M.scrollTop=M.scrollHeight}
function req(method,path,body,cb){var x=new XMLHttpRequest();x.open(method,B+path,true);if(body&&!(body instanceof FormData))x.setRequestHeader("Content-Type","text/plain");
x.onload=function(){var j=null;try{j=JSON.parse(x.responseText)}catch(e){}cb&&cb(x.status,j)};x.onerror=function(){cb&&cb(0,null)};x.send(body instanceof FormData?body:(body?JSON.stringify(body):null))}
function session(extra,cb,opening){var ref=w.waAgent&&w.waAgent.ref;var b={key:K,token:st.token,visitor_id:ck("_wa_vid"),page_url:w.location.href,ref:ref};for(var k in extra||{})b[k]=extra[k];
req("POST","/w/session",b,function(code,j){if(code!==200||!j)return;st.token=j.token;ls(TK,j.token);st.settings=j.settings||st.settings;paint();
if(opening&&st.settings.clear_on_open){M.innerHTML="";(j.messages||[]).forEach(function(m){st.seen[m.id]=1;st.last=Math.max(st.last,m.id)})}else(j.messages||[]).forEach(add);cb&&cb()})}
function initial(){var t=st.settings.initial_message;if(t&&!st.last&&!ls(TK+"_init")){ls(TK+"_init","1");send(t)}}
function prechat(){var p=st.settings.prechat||{};if(!p.enabled||ls(TK+"_pre")||st.last)return false;var f=d.createElement("form");f.className="wac-pre";
f.innerHTML='<div></div><input name="name" placeholder="Tu nombre" required><input name="email" type="email" placeholder="Tu correo"'+(p.require_email?" required":"")+'><button type="submit">Comenzar</button>';
f.firstChild.textContent=st.settings.greeting||"";f.querySelector("button").style.background=color();M.style.display="none";F.style.display="none";pan.insertBefore(f,F);
f.onsubmit=function(e){e.preventDefault();session({name:f.name.value,email:f.email.value},function(){ls(TK+"_pre","1");f.remove();M.style.display="";F.style.display=""})};return true}
function greet(){if(!st.last&&st.settings.greeting&&!M.childNodes.length){var g=d.createElement("div");g.className="wac-r wac-a";g.textContent=st.settings.greeting;M.appendChild(g)}}
function connect(){if(!st.token||st.sock)return;try{var u=B.replace(/^http/,"ws")+"/w/ws?key="+K+"&token="+encodeURIComponent(st.token)+"&after="+st.last;var so=new WebSocket(u);st.sock=so;
so.onmessage=function(e){try{var j=JSON.parse(e.data);if(j.type==="message")add(j.message)}catch(x){}};
so.onclose=function(){st.sock=null;setTimeout(function(){if(st.open)connect()},3000)}}catch(e){poll()}}
function poll(){if(!st.token||!st.open)return;req("GET","/w/messages?key="+K+"&token="+encodeURIComponent(st.token)+"&after="+st.last,null,function(c,j){(j||[]).forEach(add);setTimeout(poll,4000)})}
function send(text,file){if(!st.token){session({},function(){send(text,file)});return}var cb=function(c,j){if(c===200&&j)add(j)};
if(file){var fd=new FormData();fd.append("key",K);fd.append("token",st.token);fd.append("file",file);if(text)fd.append("text",text);req("POST","/w/messages",fd,cb)}
else req("POST","/w/messages",{key:K,token:st.token,text:text},cb)}
F.onsubmit=function(e){e.preventDefault();var t=I.value.trim();if(!t)return;I.value="";send(t)};
FI.onchange=function(){if(FI.files[0])send("",FI.files[0]);FI.value=""};
btn.onclick=function(){st.open=!st.open;pan.style.display=st.open?"flex":"none";if(st.open){session({},function(){if(!prechat()){greet();initial()}connect()},true)}else if(st.sock){st.sock.close()}};
paint();w.waChat={k:K,open:function(){if(!st.open)btn.click()}};
if(st.settings.open_selector){d.addEventListener("click",function(e){var t=e.target&&e.target.closest&&e.target.closest(st.settings.open_selector);if(t){e.preventDefault();w.waChat.open()}},true)}
if(st.settings.auto_open_s&&!(function(){try{return sessionStorage.getItem(TK+"_auto")}catch(e){return null}})()){setTimeout(function(){try{sessionStorage.setItem(TK+"_auto","1")}catch(e){}w.waChat.open()},st.settings.auto_open_s*1000)}
})(window,document);"""
