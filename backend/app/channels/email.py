"""Canal de correo (docs/data-model.md §19.1).

Entrada: (a) webhook del proveedor de correo entrante — Postmark (JSON), SendGrid Inbound Parse (multipart, crudo o
por campos) y Mailgun Routes (formulario) — en /webhooks/email/{alias}; (b) IMAP (bucle por UID, idempotente).
Cada correo se analiza con la librería estándar (`email`), el HTML se limpia (lista blanca), el texto plano se guarda
sin la parte citada y los adjuntos van a Storage. El hilo se resuelve con Message-ID / In-Reply-To / References
(email_threads) y el mensaje entra al mismo flujo que los demás canales (atribución, flujos, automatizaciones, IA).

Salida: EmailClient implementa la interfaz de app/channels (send_text, send_media…) y responde por SMTP con
In-Reply-To / References, Message-ID propio, asunto "Re: …", firma y partes texto + HTML.
"""

import asyncio
import email
import email.policy
import html
import imaplib
import logging
import re
import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.channels import ChannelError
from app.models import Channel, Contact, ContactIdentity, Conversation, EmailThread, Message, MessageWaId

log = logging.getLogger(__name__)

MAX_ATTACHMENT = 15 * 1024 * 1024
MAX_TOTAL = 25 * 1024 * 1024
MAX_TEXT = 20000


# --- Modelo del correo analizado -----------------------------------------------------------------------------
@dataclass
class Attachment:
    filename: str | None
    mime: str
    data: bytes


@dataclass
class ParsedEmail:
    from_addr: str
    from_name: str | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    date: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    text: str = ""
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


def clean_msgid(value: str | None) -> str | None:
    """Message-ID normalizado sin <> ni espacios (clave de email_threads)."""
    if not value:
        return None
    v = value.strip().strip("<>").strip()
    return v.lower() or None


def split_refs(value: str | None) -> list[str]:
    if not value:
        return []
    return [r for r in (clean_msgid(x) for x in re.findall(r"<[^>]+>", value) or value.split()) if r]


def _addresses(values: list[str]) -> list[str]:
    return [a.lower() for _n, a in getaddresses([v for v in values if v]) if a and "@" in a]


def parse_mime(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    out = ParsedEmail(from_addr=addr.lower(), from_name=name or None,
                      to=_addresses(msg.get_all("To", []) or []), cc=_addresses(msg.get_all("Cc", []) or []),
                      subject=str(msg.get("Subject", "") or "").strip(), date=str(msg.get("Date") or "") or None,
                      message_id=clean_msgid(str(msg.get("Message-ID") or "")),
                      in_reply_to=clean_msgid(str(msg.get("In-Reply-To") or "")),
                      references=split_refs(str(msg.get("References") or "")))
    total = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        ctype = part.get_content_type()
        if disp != "attachment" and ctype == "text/plain" and not out.text:
            out.text = part.get_content() or ""
        elif disp != "attachment" and ctype == "text/html" and not out.html:
            out.html = part.get_content() or ""
        elif disp in ("attachment", "inline") or not ctype.startswith("text/"):
            data = part.get_payload(decode=True) or b""
            if not data or len(data) > MAX_ATTACHMENT or total + len(data) > MAX_TOTAL:
                continue
            total += len(data)
            out.attachments.append(Attachment(part.get_filename(), ctype, data))
    if not out.text and out.html:
        out.text = html_to_text(out.html)
    return out


# --- Formatos de proveedores ---------------------------------------------------------------------------------
def from_postmark(data: dict) -> ParsedEmail:
    import base64

    headers = {h.get("Name", "").lower(): h.get("Value", "") for h in data.get("Headers") or []}
    full = data.get("FromFull") or {}
    out = ParsedEmail(from_addr=(full.get("Email") or parseaddr(data.get("From", ""))[1] or "").lower(),
                      from_name=full.get("Name") or None,
                      to=[(t.get("Email") or "").lower() for t in data.get("ToFull") or [] if t.get("Email")],
                      cc=[(t.get("Email") or "").lower() for t in data.get("CcFull") or [] if t.get("Email")],
                      subject=(data.get("Subject") or "").strip(), date=data.get("Date"),
                      message_id=clean_msgid(headers.get("message-id") or data.get("MessageID")),
                      in_reply_to=clean_msgid(headers.get("in-reply-to")),
                      references=split_refs(headers.get("references")),
                      text=data.get("TextBody") or "", html=data.get("HtmlBody") or None)
    total = 0
    for a in data.get("Attachments") or []:
        try:
            blob = base64.b64decode(a.get("Content") or "")
        except ValueError:
            continue
        if blob and len(blob) <= MAX_ATTACHMENT and total + len(blob) <= MAX_TOTAL:
            total += len(blob)
            out.attachments.append(Attachment(a.get("Name"), a.get("ContentType") or "application/octet-stream", blob))
    if not out.text and out.html:
        out.text = html_to_text(out.html)
    return out


def _headers_block(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    msg = email.message_from_string(raw.strip() + "\n\n", policy=email.policy.default)
    return {k.lower(): str(v) for k, v in msg.items()}


async def from_form(form, kind: str) -> ParsedEmail:
    """SendGrid Inbound Parse (campo `email` crudo o campos sueltos) y Mailgun Routes."""
    async def _files(prefix: str | None = None) -> list[Attachment]:
        out, total = [], 0
        for key, value in form.multi_items():
            if not hasattr(value, "read") or (prefix and not key.startswith(prefix)):
                continue
            blob = await value.read()
            if blob and len(blob) <= MAX_ATTACHMENT and total + len(blob) <= MAX_TOTAL:
                total += len(blob)
                out.append(Attachment(value.filename, value.content_type or "application/octet-stream", blob))
        return out

    if kind == "sendgrid":
        raw = form.get("email")
        if raw:  # "POST the raw, full MIME message"
            return parse_mime(raw.encode() if isinstance(raw, str) else await raw.read())
        hdrs = _headers_block(form.get("headers"))
        name, addr = parseaddr(form.get("from") or "")
        out = ParsedEmail(from_addr=addr.lower(), from_name=name or None, to=_addresses([form.get("to") or ""]),
                          cc=_addresses([form.get("cc") or ""]), subject=(form.get("subject") or "").strip(),
                          message_id=clean_msgid(hdrs.get("message-id")), in_reply_to=clean_msgid(hdrs.get("in-reply-to")),
                          references=split_refs(hdrs.get("references")), text=form.get("text") or "",
                          html=form.get("html") or None, date=hdrs.get("date"))
        out.attachments = await _files("attachment")
    else:  # mailgun
        import json

        try:
            hdr_list = json.loads(form.get("message-headers") or "[]")
        except ValueError:
            hdr_list = []
        hdrs = {str(k).lower(): str(v) for k, v in hdr_list if isinstance(hdr_list, list)} if hdr_list else {}
        name, addr = parseaddr(form.get("from") or form.get("sender") or "")
        out = ParsedEmail(from_addr=addr.lower(), from_name=name or None,
                          to=_addresses([form.get("To") or form.get("recipient") or ""]), cc=_addresses([form.get("Cc") or ""]),
                          subject=(form.get("subject") or "").strip(),
                          message_id=clean_msgid(form.get("Message-Id") or hdrs.get("message-id")),
                          in_reply_to=clean_msgid(form.get("In-Reply-To") or hdrs.get("in-reply-to")),
                          references=split_refs(form.get("References") or hdrs.get("references")),
                          text=form.get("body-plain") or "", html=form.get("body-html") or None, date=hdrs.get("date"))
        out.attachments = await _files("attachment")
    if not out.text and out.html:
        out.text = html_to_text(out.html)
    return out


# --- HTML: limpieza y texto ----------------------------------------------------------------------------------
ALLOWED_TAGS = {"p", "br", "div", "span", "b", "strong", "i", "em", "u", "a", "ul", "ol", "li", "blockquote", "table",
                "thead", "tbody", "tfoot", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "img", "hr", "pre",
                "code", "small", "sup", "sub", "font", "center"}
VOID = {"br", "img", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math", "head", "title",
                "form", "textarea", "select", "button"}
ALLOWED_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}, "td": {"colspan", "rowspan", "align"},
                 "th": {"colspan", "rowspan", "align"}, "table": {"width", "border", "cellpadding", "cellspacing"},
                 "p": {"align"}, "div": {"align"}, "font": {"color"}}
SAFE_URL = re.compile(r"^(https?:|mailto:)", re.I)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            if tag not in VOID:
                self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        keep = []
        for k, v in attrs:
            k = k.lower()
            if k not in ALLOWED_ATTRS.get(tag, set()) or v is None:
                continue
            if k in ("href", "src"):
                v = v.strip()
                if not SAFE_URL.match(v):
                    continue
            keep.append(f' {k}="{html.escape(v, quote=True)}"')
        if tag == "a":
            keep.append(' target="_blank" rel="noopener noreferrer nofollow"')
        self.out.append(f"<{tag}{''.join(keep)}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED_TAGS or tag in VOID:
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(value: str | None) -> str:
    """Lista blanca de etiquetas y atributos: sin scripts, estilos, iframes, manejadores on*, ni URLs javascript:."""
    if not value:
        return ""
    p = _Sanitizer()
    p.feed(value)
    p.close()
    return p.result()


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    p = _Text()
    p.feed(value)
    text = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


QUOTE_MARKERS = (
    re.compile(r"^\s*(On|El)\s.+(wrote|escribió)\s*:\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*(Original Message|Mensaje original)\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*(From|De)\s*:\s.+$", re.I),
    re.compile(r"^\s*_{8,}\s*$"),
    re.compile(r"^\s*(Enviado desde|Sent from) mi?\s*\w+", re.I),
)


def strip_quoted(text: str) -> str:
    """Quita la parte citada (respuestas anteriores) y la firma estándar ("-- ")."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        if line.rstrip() == "--" or any(m.match(line) for m in QUOTE_MARKERS):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return (cleaned or (text or "").strip())[:MAX_TEXT]


# --- Contactos e hilos ---------------------------------------------------------------------------------------
async def contact_for_email(session: AsyncSession, channel: Channel, address: str,
                            name: str | None) -> tuple[Contact, ContactIdentity, bool]:
    """Identidad de correo del canal; si no existe, se enlaza a un contacto que ya tenga ese correo (ficha o llave
    maestra) antes de crear uno nuevo."""
    from app.service import get_or_create_contact_by_identity

    ident = await session.scalar(select(ContactIdentity).where(
        ContactIdentity.channel_id == channel.id, ContactIdentity.external_id == address))
    if ident is None:
        existing = await session.scalar(select(Contact).where(
            Contact.organization_id == channel.organization_id, func.lower(Contact.email) == address)
            .order_by(Contact.id).limit(1))
        if existing is None:
            try:  # registro maestro (llave de correo capturada en otro canal)
                from app.models import ContactKey

                cid = await session.scalar(select(ContactKey.contact_id).where(
                    ContactKey.organization_id == channel.organization_id, ContactKey.key_type == "email",
                    ContactKey.value_normalized == address, ContactKey.status == "active").limit(1))
                existing = await session.get(Contact, cid) if cid else None
            except ImportError:  # pragma: no cover
                existing = None
        if existing is not None:
            ident = ContactIdentity(organization_id=channel.organization_id, contact_id=existing.id, provider="email",
                                    channel_id=channel.id, external_id=address, profile={"name": name} if name else {})
            session.add(ident)
            await session.flush()
            if name and not existing.name:
                existing.name = name
            return existing, ident, False
    contact, ident, is_new = await get_or_create_contact_by_identity(session, channel, address, name=name)
    if is_new and not contact.email:
        contact.email = address
    return contact, ident, is_new


async def thread_conversation(session: AsyncSession, channel: Channel, parsed: ParsedEmail) -> Conversation | None:
    ids = [i for i in [parsed.in_reply_to, *reversed(parsed.references)] if i]
    if not ids:
        return None
    t = await session.scalar(select(EmailThread).where(EmailThread.channel_id == channel.id,
                                                       EmailThread.message_id_header.in_(ids)).limit(1))
    return await session.get(Conversation, t.conversation_id) if t else None


async def remember_thread(session: AsyncSession, channel: Channel, conv: Conversation, message_id: str | None,
                          subject: str | None) -> None:
    if not message_id:
        return
    exists = await session.scalar(select(EmailThread.id).where(EmailThread.channel_id == channel.id,
                                                                EmailThread.message_id_header == message_id))
    if not exists:
        session.add(EmailThread(organization_id=channel.organization_id, channel_id=channel.id, conversation_id=conv.id,
                                message_id_header=message_id, subject=(subject or "")[:500] or None))


# --- Entrada -------------------------------------------------------------------------------------------------
async def ingest(session: AsyncSession, channel: Channel, parsed: ParsedEmail) -> Message | None:
    """Guarda el correo y corre el mismo flujo que los demás canales. Idempotente por Message-ID."""
    from app.agent import schedule_reply
    from app.db import set_actor
    from app.ingest import after_inbound
    from app.service import get_or_create_conversation, record_message, reload
    from app.models import utcnow

    if not parsed.from_addr or "@" not in parsed.from_addr:
        return None
    own = (channel.external_id or "").lower()
    if parsed.from_addr == own:
        return None  # rebote de nuestro propio envío
    mid = parsed.message_id or f"{uuid.uuid4().hex}@sin-id"
    wa_key = f"email:{mid}"
    if await session.get(MessageWaId, wa_key):
        return None
    await set_actor(session, "contact")
    contact, ident, is_new = await contact_for_email(session, channel, parsed.from_addr, parsed.from_name)
    if contact.blocked:
        await session.commit()
        return None
    now = utcnow()
    ident.last_inbound_at, contact.last_seen_at = now, now
    conv = await thread_conversation(session, channel, parsed)
    new_thread = conv is None
    if conv is not None and conv.contact_id != contact.id:
        conv = None  # otro remitente respondiendo el hilo: su propia conversación
    if conv is None:
        conv = await get_or_create_conversation(session, channel, contact)
    elif conv.status == "closed":
        conv.status, conv.assigned_agent_id = "bot", None
        await session.flush()
        await reload(session, conv)
    org = channel.organization_id
    meta: dict = {"subject": parsed.subject, "from": parsed.from_addr, "from_name": parsed.from_name, "to": parsed.to,
                  "cc": parsed.cc, "message_id": parsed.message_id, "in_reply_to": parsed.in_reply_to,
                  "references": parsed.references[-20:], "date": parsed.date, "attachments": []}
    clean = sanitize_html(parsed.html) if parsed.html else ""
    if clean:
        meta["html_path"] = await storage.upload(
            storage.new_path(storage.MEDIA_BUCKET, org, f"conv/{conv.id}/email", "text/html"), clean.encode(), "text/html")
    msg = Message(direction="in", sender_type="contact", type="email", text=strip_quoted(parsed.text) or parsed.subject,
                  wa_message_id=wa_key, status="received")
    for a in parsed.attachments:
        mime = (a.mime or "application/octet-stream").split(";")[0]
        path = await storage.upload(storage.new_path(storage.MEDIA_BUCKET, org, f"conv/{conv.id}", mime), a.data, mime)
        meta["attachments"].append({"filename": a.filename, "mime": mime, "size": len(a.data), "path": path})
        if msg.media_path is None:  # el primero queda como medio del mensaje (vista previa, lectura de documentos)
            msg.media_path, msg.media_mime, msg.media_size, msg.media_filename = path, mime, len(a.data), a.filename
    msg.metadata_ = meta
    await record_message(session, conv, msg)
    await remember_thread(session, channel, conv, parsed.message_id, parsed.subject)
    await session.commit()
    auto = (channel.settings or {}).get("auto_reply")
    if new_thread and isinstance(auto, str) and auto.strip():
        from app.service import send_text

        await send_text(session, conv, auto.strip(), sender_type="bot")
    status, conv_id, handled = await after_inbound(session, conv, msg, {}, is_new, "email")
    if status == "bot" and not handled and (channel.settings or {}).get("ai_replies", True):
        schedule_reply(conv_id)
    return msg


# --- Salida: SMTP --------------------------------------------------------------------------------------------
def smtp_send(cfg: dict, password: str | None, message: EmailMessage) -> None:
    """Envío síncrono (se llama en un hilo). Separado para poder simularlo en pruebas."""
    host, port = cfg.get("host"), int(cfg.get("port") or 587)
    if not host:
        raise ChannelError("El canal de correo no tiene servidor SMTP configurado")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if cfg.get("user"):
                s.login(cfg["user"], password or "")
            s.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=30) as s:
        if cfg.get("starttls", True):
            s.starttls()
        if cfg.get("user"):
            s.login(cfg["user"], password or "")
        s.send_message(message)


def reply_subject(subject: str | None) -> str:
    s = (subject or "").strip() or "(sin asunto)"
    return s if re.match(r"^(re|rv|fw|fwd)\s*:", s, re.I) else f"Re: {s}"


class EmailClient:
    """Interfaz de app/channels para el correo; ligado a una conversación (el destinatario es su identidad)."""

    provider = "email"

    def __init__(self, session: AsyncSession, channel: Channel, conv: Conversation | None, recipient: str | None):
        self.session = session
        self.channel = channel
        self.conv = conv
        self.recipient = recipient
        self.human = False
        self.last_meta: dict | None = None
        self._uploads: dict[str, tuple[bytes, str, str]] = {}

    async def _last_inbound(self) -> dict:
        if self.conv is None:
            return {}
        m = await self.session.scalar(
            select(Message).where(Message.conversation_id == self.conv.id, Message.direction == "in",
                                  Message.type == "email").order_by(Message.id.desc()).limit(1))
        return (m.metadata_ or {}) if m else {}

    async def send_email(self, text: str, *, subject: str | None = None, cc: list[str] | None = None,
                         html_body: str | None = None, attachments: list[tuple[bytes, str, str]] | None = None) -> str:
        from app.secrets_vault import get_secret

        to = self.recipient
        if not to:
            raise ChannelError("El contacto no tiene correo en este canal")
        s = self.channel.settings or {}
        smtp = s.get("smtp") or {}
        last = await self._last_inbound()
        sender = self.channel.external_id or smtp.get("user") or ""
        domain = sender.split("@")[-1] if "@" in sender else "wa-agent.local"
        msgid = make_msgid(domain=domain)
        m = EmailMessage()
        m["From"] = formataddr((s.get("from_name") or self.channel.name, sender))
        m["To"] = to
        if cc:
            m["Cc"] = ", ".join(cc)
        m["Subject"] = subject or reply_subject(last.get("subject"))
        m["Message-ID"] = msgid
        if last.get("message_id"):
            m["In-Reply-To"] = f"<{last['message_id']}>"
            refs = [*(last.get("references") or []), last["message_id"]]
            m["References"] = " ".join(f"<{r}>" for r in refs[-20:])
        signature_html = s.get("signature_html") or ""
        sig_text = html_to_text(signature_html)
        m.set_content(text + (f"\n\n-- \n{sig_text}" if sig_text else ""))
        body_html = html_body or "<br>".join(html.escape(line) for line in text.split("\n"))
        m.add_alternative(f"<div>{sanitize_html(body_html)}</div>"
                          + (f"<br><div>{sanitize_html(signature_html)}</div>" if signature_html else ""), subtype="html")
        for data, mime, filename in attachments or []:
            main, _, sub = mime.partition("/")
            m.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=filename)
        password = await get_secret(self.session, smtp.get("password_secret_id"))
        await asyncio.to_thread(smtp_send, smtp, password, m)
        clean_id = clean_msgid(msgid)
        self.last_meta = {"subject": str(m["Subject"]), "from": sender, "to": [to], "cc": cc or [],
                          "message_id": clean_id, "in_reply_to": last.get("message_id"), "outbound": True}
        if self.conv is not None:
            await remember_thread(self.session, self.channel, self.conv, clean_id, str(m["Subject"]))
        return f"email:{clean_id}"

    async def send_text(self, to, body: str) -> list[str]:
        return [await self.send_email(body)]

    async def send_buttons(self, to, body: str, buttons: list[dict]) -> str:
        opts = "\n".join(f"{i}. {b.get('title', b) if isinstance(b, dict) else b}" for i, b in enumerate(buttons, 1))
        return await self.send_email(f"{body}\n\n{opts}")

    async def send_list(self, to, body: str, button: str, sections: list[dict]) -> str:
        rows = [r.get("title", "") for sec in sections for r in sec.get("rows", [])]
        return await self.send_email(body + "\n\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(rows, 1)))

    async def send_image_link(self, to, link: str, caption: str | None = None) -> str:
        return await self.send_email(f"{caption or ''}\n{link}".strip())

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        key = uuid.uuid4().hex
        self._uploads[key] = (data, mime, filename)
        return key

    async def send_media(self, to, kind: str, media_id: str, caption: str | None = None,
                         filename: str | None = None) -> str:
        att = self._uploads.pop(media_id, None)
        return await self.send_email(caption or (filename or "Adjunto"), attachments=[att] if att else None)

    async def send_product(self, to, catalog_id: str, retailer_id: str, body: str) -> str:
        return await self.send_email(body)

    async def send_template(self, to, name: str, language: str, components: list[dict]) -> str:
        raise ChannelError("Las plantillas son solo de WhatsApp")

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        raise ChannelError("Los adjuntos del correo se guardan al recibirlo")

    async def mark_read(self, _message_id: str | None = None) -> None:
        return None


# --- IMAP ----------------------------------------------------------------------------------------------------
def imap_fetch(cfg: dict, password: str | None, last_uid: int, limit: int = 50) -> list[tuple[int, bytes]]:
    """Correos con UID > last_uid (síncrono, se llama en un hilo). Separado para poder simularlo."""
    host, port = cfg.get("host"), int(cfg.get("port") or 993)
    conn = imaplib.IMAP4_SSL(host, port) if cfg.get("ssl", True) else imaplib.IMAP4(host, port)
    try:
        conn.login(cfg.get("user") or "", password or "")
        conn.select(cfg.get("folder") or "INBOX", readonly=True)
        typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)[:limit]
        out = []
        for uid in uids:
            typ, parts = conn.uid("fetch", str(uid), "(RFC822)")
            if typ == "OK":
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
                if raw:
                    out.append((uid, raw))
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


async def poll_channel(session: AsyncSession, channel: Channel) -> int:
    from app.secrets_vault import get_secret

    s = dict(channel.settings or {})
    cfg = s.get("imap") or {}
    if not cfg.get("host"):
        return 0
    password = await get_secret(session, cfg.get("password_secret_id"))
    last = int(s.get("imap_last_uid") or 0)
    items = await asyncio.to_thread(imap_fetch, cfg, password, last)
    n = 0
    for uid, raw in items:
        try:
            if await ingest(session, channel, parse_mime(raw)):
                n += 1
        except Exception:  # noqa: BLE001 — un correo malo no detiene la bandeja
            log.exception("No se pudo procesar el correo UID %s del canal %s", uid, channel.id)
            await session.rollback()
        last = max(last, uid)
        channel = await session.get(Channel, channel.id)
        channel.settings = {**(channel.settings or {}), "imap_last_uid": last}
        await session.commit()
    return n


async def imap_loop() -> None:
    """Revisa las bandejas IMAP cada minuto (solo canales de correo con entrada imap activos)."""
    from app.db import SessionLocal

    while True:
        try:
            async with SessionLocal() as session:
                channels = (await session.scalars(select(Channel).where(
                    Channel.provider == "email", Channel.status == "active",
                    Channel.settings["inbound"].astext == "imap"))).all()
                for ch in channels:
                    try:
                        await poll_channel(session, ch)
                        if ch.last_error:
                            ch.last_error = None
                            await session.commit()
                    except Exception as e:  # noqa: BLE001
                        log.warning("IMAP del canal %s falló: %s", ch.id, e)
                        await session.rollback()
                        ch = await session.get(Channel, ch.id)
                        ch.last_error = f"IMAP: {e}"[:1000]
                        await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("Bucle IMAP falló")
        await asyncio.sleep(60)
