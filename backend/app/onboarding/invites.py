"""Invitaciones de usuarios: token de un solo uso (solo se guarda su sha256), correo por SMTP si está configurado
y aceptación pública que crea el usuario con el rol y los grupos elegidos."""

import asyncio
import hashlib
import logging
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AgentInvitation, Organization, utcnow

log = logging.getLogger(__name__)
TTL = timedelta(days=7)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def link(token: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}/invitacion/{token}"


def status(inv: AgentInvitation) -> str:
    if inv.accepted_at:
        return "accepted"
    if inv.revoked_at:
        return "revoked"
    if inv.expires_at <= utcnow():
        return "expired"
    return "pending"


def _send_sync(to: str, subject: str, text: str) -> None:
    env = get_settings()
    msg = EmailMessage()
    msg["From"] = env.smtp_from or env.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    with smtplib.SMTP(env.smtp_host, env.smtp_port, timeout=20) as s:
        if env.smtp_starttls:
            s.starttls()
        if env.smtp_user:
            s.login(env.smtp_user, env.smtp_password)
        s.send_message(msg)


async def send_email(to: str, subject: str, text: str) -> bool:
    """True si se envió. Sin SMTP configurado devuelve False (el panel muestra el enlace)."""
    if not get_settings().smtp_host:
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, text)
        return True
    except (OSError, smtplib.SMTPException) as e:
        log.warning("No se pudo enviar la invitación a %s: %s", to, e)
        return False


async def pending_count(session: AsyncSession, org: int) -> int:
    return await session.scalar(select(func.count()).select_from(AgentInvitation).where(
        AgentInvitation.organization_id == org, AgentInvitation.accepted_at.is_(None),
        AgentInvitation.revoked_at.is_(None), AgentInvitation.expires_at > utcnow())) or 0


async def by_token(session: AsyncSession, token: str) -> AgentInvitation | None:
    return await session.scalar(select(AgentInvitation).where(AgentInvitation.token_hash == token_hash(token)))


async def email_text(session: AsyncSession, inv: AgentInvitation, token: str, inviter: str) -> tuple[str, str]:
    org = await session.get(Organization, inv.organization_id)
    role = {"admin": "administrador", "supervisor": "supervisor", "agent": "asesor"}.get(inv.role, inv.role)
    subject = f"{inviter} te invitó a {org.name}"
    body = (f"Hola{' ' + inv.name if inv.name else ''},\n\n{inviter} te invitó a unirte a {org.name} como {role} "
            f"para atender clientes por WhatsApp.\n\nAcepta la invitación aquí (vence en 7 días):\n{link(token)}\n")
    return subject, body
