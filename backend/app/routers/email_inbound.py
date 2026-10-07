"""Canal de correo: alta y configuración (panel), webhook de correo entrante (público) y lectura/respuesta de
correos en la bandeja. docs/data-model.md §19.1

Webhook: /webhooks/email/{alias}. El alias es aleatorio y único por canal (es el secreto de la URL). Acepta:
- Postmark Inbound (JSON con FromFull, TextBody, HtmlBody, Headers, Attachments en base64)
- SendGrid Inbound Parse (multipart; con "POST the raw MIME" llega el campo `email`, si no, campos sueltos)
- Mailgun Routes → forward (formulario con body-plain, body-html, Message-Id, attachment-N); si el canal tiene
  `mailgun_signing_key`, se valida la firma (timestamp + token, HMAC-SHA256).
"""

import hashlib
import hmac
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import current_agent, require_admin
from app.channels import email as mail
from app.config import get_settings
from app.db import SessionLocal, get_session, set_actor
from app.models import Agent, Channel, InboundEvent, utcnow
from app.plans import enforce_limit, has_feature
from app.secrets_vault import get_secret, put_secret

router = APIRouter(tags=["email"])
log = logging.getLogger(__name__)
env = get_settings()


# --- Panel: alta y configuración -----------------------------------------------------------------------------
class SmtpIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    user: str | None = None
    password: str | None = None      # vacío al editar = conservar
    starttls: bool = True


class ImapIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=993, ge=1, le=65535)
    user: str | None = None
    password: str | None = None
    folder: str = "INBOX"
    ssl: bool = True


class EmailChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=3, max_length=254)
    inbound: str = Field(default="forward", pattern="^(forward|imap|provider)$")
    smtp: SmtpIn | None = None
    imap: ImapIn | None = None
    from_name: str | None = None
    signature_html: str | None = None
    group_id: int | None = None
    auto_reply: str | None = None
    ai_replies: bool = True
    bot_id: int | None = None
    mailgun_signing_key: str | None = None


def _addr(raw: str) -> str:
    a = raw.strip().lower()
    if "@" not in a or " " in a or a.startswith("@") or a.endswith("@"):
        raise HTTPException(422, "Correo inválido")
    return a


def inbound_url(c: Channel) -> str:
    return f"{env.public_base_url.rstrip('/')}/webhooks/email/{(c.settings or {}).get('inbound_alias', '')}"


def channel_out(c: Channel) -> dict:
    s = c.settings or {}
    domain = getattr(env, "email_inbound_domain", "") or ""
    return {"id": c.id, "name": c.name, "provider": c.provider, "address": c.external_id, "status": c.status,
            "last_error": c.last_error, "bot_id": c.default_ai_agent_id, "inbound": s.get("inbound"),
            "inbound_url": inbound_url(c),
            "forward_address": f"{s.get('inbound_alias')}@{domain}" if domain and s.get("inbound_alias") else None,
            "smtp": {k: v for k, v in (s.get("smtp") or {}).items() if k != "password_secret_id"}
            | {"has_password": bool((s.get("smtp") or {}).get("password_secret_id"))},
            "imap": {k: v for k, v in (s.get("imap") or {}).items() if k != "password_secret_id"}
            | {"has_password": bool((s.get("imap") or {}).get("password_secret_id"))} if s.get("imap") else None,
            "from_name": s.get("from_name"), "signature_html": s.get("signature_html"), "group_id": s.get("group_id"),
            "auto_reply": s.get("auto_reply"), "ai_replies": s.get("ai_replies", True),
            "mailgun_signing": bool(s.get("mailgun_key_secret_id"))}


async def _apply(session: AsyncSession, c: Channel, body: EmailChannelIn) -> None:
    from app.routers.config import _check_bot

    s = dict(c.settings or {})
    s.setdefault("inbound_alias", secrets.token_urlsafe(18))
    s["inbound"] = body.inbound
    if body.smtp is not None:
        cur = s.get("smtp") or {}
        smtp = body.smtp.model_dump(exclude={"password"})
        smtp["password_secret_id"] = cur.get("password_secret_id")
        if body.smtp.password:
            smtp["password_secret_id"] = await put_secret(session, body.smtp.password, f"email_smtp:{c.id}",
                                                          cur.get("password_secret_id"))
        s["smtp"] = smtp
    if body.imap is not None:
        cur = s.get("imap") or {}
        imap = body.imap.model_dump(exclude={"password"})
        imap["password_secret_id"] = cur.get("password_secret_id")
        if body.imap.password:
            imap["password_secret_id"] = await put_secret(session, body.imap.password, f"email_imap:{c.id}",
                                                          cur.get("password_secret_id"))
        s["imap"] = imap
    if body.inbound == "imap" and not (s.get("imap") or {}).get("host"):
        raise HTTPException(422, "Para recibir por IMAP indica el servidor IMAP")
    if body.mailgun_signing_key:
        s["mailgun_key_secret_id"] = await put_secret(session, body.mailgun_signing_key, f"email_mailgun:{c.id}",
                                                      s.get("mailgun_key_secret_id"))
    if body.group_id is not None:
        from app.models import Group

        g = await session.get(Group, body.group_id)
        if not g or g.organization_id != c.organization_id:
            raise HTTPException(404, "Grupo no encontrado")
    s.update({"from_name": (body.from_name or "").strip()[:120] or None,
              "signature_html": mail.sanitize_html(body.signature_html)[:10000] if body.signature_html else None,
              "group_id": body.group_id, "auto_reply": (body.auto_reply or "").strip()[:2000] or None,
              "ai_replies": body.ai_replies})
    c.settings = s
    if body.bot_id is not None:
        c.default_ai_agent_id = await _check_bot(session, c.organization_id, body.bot_id)


async def _get_email_channel(session: AsyncSession, org: int, cid: int) -> Channel:
    c = await session.get(Channel, cid)
    if not c or c.organization_id != org:
        raise HTTPException(404, "Canal no encontrado")
    if c.provider != "email":
        raise HTTPException(409, "El canal no es de correo")
    return c


@router.post("/api/channels/email")
async def create_email_channel(body: EmailChannelIn, agent: Agent = Depends(require_admin),
                               session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    if not await has_feature(session, org, "omnichannel"):
        raise HTTPException(402, "Tu plan no incluye «Omnicanal» (correo, Instagram, Messenger y chat web)")
    await enforce_limit(session, org, "channels")
    address = _addr(body.address)
    if await session.scalar(select(Channel.id).where(Channel.provider == "email", Channel.external_id == address)):
        raise HTTPException(409, "Ese correo ya está conectado")
    from app.routers.omnichannel import _default_bot

    c = Channel(organization_id=org, name=body.name.strip(), provider="email", external_id=address, settings={},
                default_ai_agent_id=await _default_bot(session, org, body.bot_id))
    session.add(c)
    await session.flush()
    await _apply(session, c, body)
    await session.commit()
    return channel_out(c)


@router.put("/api/channels/{cid}/email")
async def update_email_channel(cid: int, body: EmailChannelIn, agent: Agent = Depends(require_admin),
                               session: AsyncSession = Depends(get_session)):
    c = await _get_email_channel(session, agent.organization_id, cid)
    address = _addr(body.address)
    if address != c.external_id and await session.scalar(
            select(Channel.id).where(Channel.provider == "email", Channel.external_id == address)):
        raise HTTPException(409, "Ese correo ya está conectado")
    c.name, c.external_id = body.name.strip(), address
    await _apply(session, c, body)
    await session.commit()
    return channel_out(c)


@router.get("/api/channels/{cid}/email")
async def get_email_channel(cid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return channel_out(await _get_email_channel(session, agent.organization_id, cid))


@router.post("/api/channels/{cid}/email/test")
async def test_email_channel(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Prueba SMTP (conexión + inicio de sesión) e IMAP si está configurado. No envía correos."""
    import asyncio
    import smtplib

    c = await _get_email_channel(session, agent.organization_id, cid)
    s = c.settings or {}
    out: dict = {}
    smtp = s.get("smtp") or {}
    if smtp.get("host"):
        pwd = await get_secret(session, smtp.get("password_secret_id"))

        def _smtp() -> None:
            port = int(smtp.get("port") or 587)
            cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
            with cls(smtp["host"], port, timeout=15) as srv:
                if port != 465 and smtp.get("starttls", True):
                    srv.starttls()
                if smtp.get("user"):
                    srv.login(smtp["user"], pwd or "")
        try:
            await asyncio.to_thread(_smtp)
            out["smtp"] = {"ok": True}
        except Exception as e:  # noqa: BLE001
            out["smtp"] = {"ok": False, "error": str(e)[:300]}
    imap = s.get("imap") or {}
    if imap.get("host"):
        pwd = await get_secret(session, imap.get("password_secret_id"))
        try:
            await asyncio.to_thread(mail.imap_fetch, imap, pwd, 10**12, 0)
            out["imap"] = {"ok": True}
        except Exception as e:  # noqa: BLE001
            out["imap"] = {"ok": False, "error": str(e)[:300]}
    return out


# --- Bandeja: correo completo y respuesta con asunto / CC ------------------------------------------------------
@router.get("/api/messages/{message_id}/email")
async def email_detail(message_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    from app.routers.inbox import _conversation
    from app.service import get_message

    m = await get_message(session, message_id)
    if not m or m.organization_id != agent.organization_id or m.type != "email":
        raise HTTPException(404, "Correo no encontrado")
    await _conversation(session, m.conversation_id, agent)  # alcance del usuario
    meta = m.metadata_ or {}
    html_body = None
    if meta.get("html_path"):
        try:
            html_body = (await storage.download(meta["html_path"])).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            html_body = None
    return {"id": m.id, "subject": meta.get("subject"), "from": meta.get("from"), "from_name": meta.get("from_name"),
            "to": meta.get("to") or [], "cc": meta.get("cc") or [], "date": meta.get("date"), "text": m.text,
            "html": mail.sanitize_html(html_body) if html_body else None,  # se vuelve a limpiar al servir
            "attachments": [{"index": i, "filename": a.get("filename"), "mime": a.get("mime"), "size": a.get("size")}
                            for i, a in enumerate(meta.get("attachments") or [])]}


@router.get("/api/messages/{message_id}/email/attachments/{index}")
async def email_attachment(message_id: int, index: int, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    from app.routers.inbox import _conversation
    from app.service import get_message

    m = await get_message(session, message_id)
    if not m or m.organization_id != agent.organization_id or m.type != "email":
        raise HTTPException(404, "Correo no encontrado")
    await _conversation(session, m.conversation_id, agent)
    atts = (m.metadata_ or {}).get("attachments") or []
    if not 0 <= index < len(atts):
        raise HTTPException(404, "Adjunto no encontrado")
    a = atts[index]
    name = (a.get("filename") or "adjunto").replace('"', "")
    return Response(await storage.download(a["path"]), media_type=a.get("mime") or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class EmailReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    subject: str | None = Field(default=None, max_length=500)
    cc: list[str] = []
    html: str | None = None


@router.post("/api/conversations/{conv_id}/email")
async def reply_email(conv_id: int, body: EmailReplyIn, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    """Respuesta del asesor con asunto y CC (el envío normal de texto también funciona y responde el hilo)."""
    from app.channels import channel_client
    from app.models import Message
    from app.routers.inbox import _conversation
    from app.service import message_out, record_message

    conv = await _conversation(session, conv_id, agent)
    if conv.channel.provider != "email":
        raise HTTPException(409, "La conversación no es de correo")
    cc = [_addr(x) for x in body.cc if x.strip()][:10]
    await set_actor(session, "agent", agent.id)
    msg = Message(direction="out", sender_type="agent", sender_agent_id=agent.id, type="email", text=body.text.strip())
    try:
        client = await channel_client(session, conv.channel, conv)
        msg.wa_message_id = await client.send_email(body.text.strip(), subject=body.subject or None, cc=cc,
                                                    html_body=body.html)
        msg.status, msg.metadata_ = "sent", client.last_meta
    except Exception as e:  # noqa: BLE001
        msg.status, msg.error = "failed", str(e)[:2000]
    await record_message(session, conv, msg)
    return message_out(msg)


# --- Webhook público de correo entrante ----------------------------------------------------------------------
async def _limit(session: AsyncSession, request: Request) -> None:
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
    n = await session.scalar(text("select public.rate_limit_hit(:b, 60)"), {"b": f"email-in:{ip}"})
    await session.commit()
    if n and n > 300:
        raise HTTPException(429, "Demasiadas solicitudes")


def _mailgun_ok(key: str, form) -> bool:
    ts, token, sig = form.get("timestamp") or "", form.get("token") or "", form.get("signature") or ""
    try:
        if abs(time.time() - int(ts)) > 15 * 60:
            return False
    except ValueError:
        return False
    digest = hmac.new(key.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig)


@router.post("/webhooks/email/{alias}")
async def email_webhook(alias: str, request: Request):
    async with SessionLocal() as session:
        await _limit(session, request)
        channel = await session.scalar(select(Channel).where(
            Channel.provider == "email", Channel.settings["inbound_alias"].astext == alias))
        if not channel or channel.status != "active":
            raise HTTPException(404, "Buzón no encontrado")
        ctype = (request.headers.get("content-type") or "").lower()
        raw_log: dict
        if "application/json" in ctype:
            data = await request.json()
            parsed = mail.from_postmark(data if isinstance(data, dict) else {})
            raw_log = {"provider": "postmark", "subject": parsed.subject, "from": parsed.from_addr,
                       "message_id": parsed.message_id}
        else:
            form = await request.form()
            kind = "mailgun" if ("body-plain" in form or "Message-Id" in form or "message-headers" in form
                                 or "signature" in form) else "sendgrid"
            if kind == "mailgun" and (channel.settings or {}).get("mailgun_key_secret_id"):
                key = await get_secret(session, channel.settings["mailgun_key_secret_id"])
                if not key or not _mailgun_ok(key, form):
                    raise HTTPException(401, "Firma de Mailgun inválida")
            parsed = await mail.from_form(form, kind)
            raw_log = {"provider": kind, "subject": parsed.subject, "from": parsed.from_addr,
                       "message_id": parsed.message_id}
        ev = InboundEvent(organization_id=channel.organization_id, source="email", payload=raw_log)
        session.add(ev)
        await session.commit()
        try:
            await mail.ingest(session, channel, parsed)
            ev.processed_at = utcnow()
        except Exception as e:  # noqa: BLE001 — se registra y se responde 200 para que el proveedor no reintente sin fin
            log.exception("Correo entrante falló en el canal %s", channel.id)
            await session.rollback()
            ev = await session.get(InboundEvent, (ev.id, ev.received_at))
            ev.error = str(e)[:2000]
        await session.commit()
    return JSONResponse({"ok": True})


@router.get("/api/channels/{cid}/email/setup")
async def email_setup(cid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Instrucciones para conectar el proveedor de correo entrante."""
    c = await _get_email_channel(session, agent.organization_id, cid)
    url = inbound_url(c)
    return {"inbound_url": url, "providers": [
        {"provider": "postmark", "steps": f"Servidor → Inbound → Webhook URL: {url}. Reenvía tu buzón a la dirección "
                                          "de entrada de Postmark."},
        {"provider": "sendgrid", "steps": f"Settings → Inbound Parse → Add Host & URL: {url} (marca «POST the raw, full "
                                          "MIME message»). Apunta el MX del subdominio a mx.sendgrid.net."},
        {"provider": "mailgun", "steps": f"Receiving → Create route → forward(\"{url}\"). Copia la «HTTP webhook "
                                         "signing key» en el canal para validar la firma."},
        {"provider": "imap", "steps": "Elige «IMAP» y escribe servidor, usuario y contraseña de aplicación: revisamos "
                                      "la bandeja cada minuto."}]}

