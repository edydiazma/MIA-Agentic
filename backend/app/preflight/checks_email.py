"""Correo: SMTP del servidor (invitaciones, 2FA, recuperación), canales de correo (SMTP/IMAP), entrada y MX.

Nunca se envía un correo: SMTP se prueba hasta AUTH y se cierra; IMAP hasta seleccionar la carpeta.
"""

import asyncio
import imaplib
import smtplib
from urllib.parse import urlparse

from sqlalchemy import select

from app.models import Channel
from app.preflight.core import Context, Result, check, skipped
from app.secrets_vault import get_secret


def smtp_probe(host: str, port: int, user: str | None, password: str | None, starttls: bool = True) -> str:
    """Conecta, TLS, AUTH y QUIT. Devuelve el saludo del servidor. Síncrona: se llama en un hilo."""
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10) as s:
            if user:
                s.login(user, password or "")
            return "SMTPS"
    with smtplib.SMTP(host, port, timeout=10) as s:
        s.ehlo()
        if starttls:
            s.starttls()
            s.ehlo()
        if user:
            s.login(user, password or "")
        return "STARTTLS" if starttls else "SMTP"


def imap_probe(host: str, port: int, user: str, password: str | None, folder: str, ssl: bool = True) -> int:
    """Login + SELECT de solo lectura. Devuelve cuántos mensajes hay en la carpeta."""
    conn = imaplib.IMAP4_SSL(host, port, timeout=10) if ssl else imaplib.IMAP4(host, port, timeout=10)
    try:
        conn.login(user, password or "")
        typ, data = conn.select(folder or "INBOX", readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"carpeta {folder!r} no disponible")
        return int(data[0] or 0)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def mx_lookup(domain: str) -> list[str] | None:
    """Registros MX (None si dnspython no está instalado)."""
    try:
        import dns.resolver
    except ImportError:
        return None
    try:
        return sorted(str(r.exchange).rstrip(".") for r in dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:  # noqa: BLE001
        return []


def _smtp_fix(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "usuario o contraseña rechazados (en Gmail/Microsoft 365 usa una contraseña de aplicación)"
    if isinstance(e, TimeoutError | OSError):
        return "no hay conexión con el servidor (puerto bloqueado: en EC2 el puerto 25 está cerrado, usa 587 o 465)"
    return type(e).__name__


@check("email", "email.smtp")
async def server_smtp(ctx: Context) -> list[Result]:
    label = "SMTP del servidor (invitaciones, 2FA, recuperación)"
    s = ctx.settings
    if not s.smtp_host:
        return skipped("email", "email.smtp", label,
                       "Sin SMTP_HOST: las invitaciones muestran el enlace para copiarlo y los códigos 2FA por correo "
                       "y la recuperación de contraseña no se envían.")
    from app.preflight import checks_email

    try:
        mode = await asyncio.to_thread(checks_email.smtp_probe, s.smtp_host, int(s.smtp_port), s.smtp_user or None,
                                       s.smtp_password or None, bool(s.smtp_starttls))
    except Exception as e:  # noqa: BLE001
        return [Result("email", "email.smtp", label, "fail", f"No se pudo autenticar: {_smtp_fix(e)}.",
                       {"host": s.smtp_host, "port": s.smtp_port})]
    status = "pass" if s.smtp_from else "warn"
    return [Result("email", "email.smtp", label, status,
                   f"Conexión {mode} y autenticación correctas." + ("" if s.smtp_from else
                                                                     " Define SMTP_FROM (remitente)."),
                   {"host": s.smtp_host, "port": s.smtp_port})]


@check("email", "email.inbound_domain")
async def inbound_domain(ctx: Context) -> list[Result]:
    label = "Dominio de entrada de correo (EMAIL_INBOUND_DOMAIN)"
    domain = getattr(ctx.settings, "email_inbound_domain", "")
    if not domain:
        return skipped("email", "email.inbound_domain", label,
                       "Sin dominio de reenvío propio: cada canal usa la URL de su proveedor (Postmark, SendGrid, "
                       "Mailgun) o IMAP.")
    from app.preflight import checks_email

    mx = await asyncio.to_thread(checks_email.mx_lookup, domain)
    if mx is None:
        return skipped("email", "email.inbound_domain", label, "dnspython no está instalado en el servidor.")
    return [Result("email", "email.inbound_domain", label, "pass" if mx else "fail",
                   f"MX: {', '.join(mx)}." if mx else
                   f"{domain} no tiene registros MX: apúntalo al proveedor de entrada (ej. mx.sendgrid.net o "
                   "inbound.postmarkapp.com).", {"domain": domain, "mx": mx})]


@check("email", "email.channels", scope="org")
async def email_channels(ctx: Context) -> list[Result]:
    async with ctx.db() as s:
        channels = (await s.scalars(select(Channel).where(
            Channel.organization_id == ctx.organization_id, Channel.provider == "email"))).unique().all()
        secrets = {}
        for c in channels:
            st = c.settings or {}
            secrets[c.id] = (await get_secret(s, (st.get("smtp") or {}).get("password_secret_id")),
                             await get_secret(s, (st.get("imap") or {}).get("password_secret_id")))
    if not channels:
        return skipped("email", "email.channels", "Canales de correo", "La empresa no tiene canales de correo.")
    from app.preflight import checks_email

    out: list[Result] = []
    base = ctx.settings.public_base_url.rstrip("/")
    for c in channels:
        st, sfx, name = c.settings or {}, f":{c.id}", c.name or c.external_id
        smtp_pw, imap_pw = secrets[c.id]
        smtp = st.get("smtp") or {}
        if smtp.get("host"):
            try:
                await asyncio.to_thread(checks_email.smtp_probe, smtp["host"], int(smtp.get("port") or 587),
                                        smtp.get("user"), smtp_pw, bool(smtp.get("starttls", True)))
                out.append(Result("email", f"email.channel_smtp{sfx}", f"{name}: envío (SMTP)", "pass",
                                  "Autenticación correcta.", {"channel_id": c.id, "host": smtp["host"]}))
            except Exception as e:  # noqa: BLE001
                out.append(Result("email", f"email.channel_smtp{sfx}", f"{name}: envío (SMTP)", "fail",
                                  f"No se pudo autenticar: {_smtp_fix(e)}.", {"channel_id": c.id, "host": smtp["host"]}))
        else:
            out.append(Result("email", f"email.channel_smtp{sfx}", f"{name}: envío (SMTP)", "fail",
                              "El canal no tiene servidor SMTP: las respuestas no saldrán.", {"channel_id": c.id}))
        inbound = st.get("inbound")
        if inbound == "imap":
            imap = st.get("imap") or {}
            try:
                n = await asyncio.to_thread(checks_email.imap_probe, imap.get("host") or "", int(imap.get("port") or 993),
                                            imap.get("user") or "", imap_pw, imap.get("folder") or "INBOX",
                                            bool(imap.get("ssl", True)))
                out.append(Result("email", f"email.channel_imap{sfx}", f"{name}: recepción (IMAP)", "pass",
                                  f"Bandeja accesible ({n} correos).", {"channel_id": c.id}))
            except Exception as e:  # noqa: BLE001
                out.append(Result("email", f"email.channel_imap{sfx}", f"{name}: recepción (IMAP)", "fail",
                                  f"No se pudo abrir la bandeja ({type(e).__name__}): revisa host, puerto 993, "
                                  "usuario y contraseña de aplicación; en Gmail habilita IMAP.", {"channel_id": c.id}))
        elif st.get("inbound_alias"):
            url = f"{base}/webhooks/email/{st['inbound_alias']}"
            host = urlparse(base).hostname or ""
            if host in ("localhost", "127.0.0.1"):
                out.append(Result("email", f"email.channel_inbound{sfx}", f"{name}: recepción (webhook)", "skipped",
                                  "PUBLIC_BASE_URL es local: el proveedor no puede llamar al webhook.",
                                  {"channel_id": c.id}))
                continue
            try:
                async with ctx.http(6) as http:
                    r = await http.get(url)
                ok = r.status_code < 500 and r.status_code != 404
            except Exception:  # noqa: BLE001
                ok = False
            out.append(Result("email", f"email.channel_inbound{sfx}", f"{name}: recepción (webhook)",
                              "pass" if ok else "fail",
                              "La URL de entrada responde desde internet." if ok else
                              "La URL de entrada no responde: revisa que Caddy enrute /webhooks/* al backend y el DNS.",
                              {"channel_id": c.id}))
    return out
