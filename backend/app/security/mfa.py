"""Segundo factor: TOTP (app autenticadora) o código por correo, códigos de recuperación y dispositivos de confianza.

Flujo: el login con contraseña válida devuelve un `mfa_token` (reto intermedio, 10 min, 5 intentos) en vez del
token de acceso; `POST /api/auth/mfa/verify` lo canjea con el código. Si el navegador presenta un token de
dispositivo de confianza vigente para el mismo usuario y la misma IP, no se vuelve a pedir el código.
"""

import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, MfaChallenge, TrustedDevice, utcnow
from app.secrets_vault import get_secret
from app.security import crypto, totp

CHALLENGE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RECOVERY_CODES = 10


def methods_for(agent: Agent) -> list[str]:
    """Métodos disponibles para el reto. Sin 2FA activado pero exigido por política: correo."""
    if agent.mfa_method == "totp" and agent.mfa_secret_id:
        return ["totp", "recovery"]
    return ["email", "recovery"] if agent.mfa_recovery_hashes else ["email"]


async def start_challenge(session: AsyncSession, agent: Agent, ip: str | None) -> tuple[str, list[str], str | None]:
    """Crea el reto. Devuelve (mfa_token, métodos, código_email_en_claro o None)."""
    methods = methods_for(agent)
    raw = crypto.random_token()
    method = methods[0]
    code = None
    challenge = MfaChallenge(agent_id=agent.id, method=method, token_hash=crypto.sha256(raw), ip=ip,
                             expires_at=utcnow() + CHALLENGE_TTL)
    if method == "email":
        code = f"{secrets.randbelow(10 ** 6):06d}"
        challenge.code_hash = crypto.sha256(f"{agent.id}:{code}")
    session.add(challenge)
    await session.flush()
    return raw, methods, code


async def load_challenge(session: AsyncSession, raw_token: str) -> MfaChallenge | None:
    ch = await session.scalar(select(MfaChallenge).where(MfaChallenge.token_hash == crypto.sha256(raw_token or "")))
    if not ch or ch.verified_at or ch.expires_at <= utcnow() or ch.attempts >= MAX_ATTEMPTS:
        return None
    return ch


async def check_code(session: AsyncSession, agent: Agent, ch: MfaChallenge, code: str) -> str | None:
    """Verifica el código; devuelve el método que funcionó ('totp' | 'email' | 'recovery') o None."""
    ch.attempts += 1
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if ch.method == "totp" and agent.mfa_secret_id:
        secret = await get_secret(session, agent.mfa_secret_id)
        if secret and totp.verify(secret, code):
            return "totp"
    if ch.method == "email" and ch.code_hash and crypto.equal(ch.code_hash, crypto.sha256(f"{agent.id}:{code}")):
        return "email"
    # Código de recuperación (un solo uso)
    h = crypto.sha256(f"recovery:{agent.id}:{code.lower()}")
    hashes = list(agent.mfa_recovery_hashes or [])
    match = next((x for x in hashes if crypto.equal(x, h)), None)
    if match:
        hashes.remove(match)
        agent.mfa_recovery_hashes = hashes
        return "recovery"
    return None


def new_recovery_codes(agent: Agent) -> list[str]:
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(RECOVERY_CODES)]
    agent.mfa_recovery_hashes = [crypto.sha256(f"recovery:{agent.id}:{c.replace('-', '')}") for c in codes]
    return codes


async def trusted(session: AsyncSession, agent: Agent, device_token: str | None, ip: str | None) -> bool:
    if not device_token:
        return False
    dev = await session.scalar(select(TrustedDevice).where(
        TrustedDevice.agent_id == agent.id, TrustedDevice.device_hash == crypto.sha256(device_token)))
    if not dev or dev.expires_at <= utcnow():
        return False
    if dev.ip and ip and dev.ip != ip:
        return False  # otra IP: se vuelve a pedir el código (y se actualiza al verificar)
    dev.last_used_at = utcnow()
    return True


async def remember(session: AsyncSession, agent: Agent, ip: str | None, user_agent: str | None, days: int,
                   device_token: str | None = None) -> str | None:
    if days <= 0:
        return None
    raw = device_token or crypto.random_token()
    h = crypto.sha256(raw)
    dev = await session.scalar(select(TrustedDevice).where(TrustedDevice.agent_id == agent.id,
                                                           TrustedDevice.device_hash == h))
    expires = utcnow() + timedelta(days=days)
    if dev:
        dev.ip, dev.user_agent, dev.expires_at, dev.last_used_at = ip, user_agent, expires, utcnow()
    else:
        session.add(TrustedDevice(agent_id=agent.id, device_hash=h, ip=ip, user_agent=user_agent, expires_at=expires))
    return raw


async def forget_devices(session: AsyncSession, agent_id: int) -> None:
    await session.execute(update(TrustedDevice).where(TrustedDevice.agent_id == agent_id)
                          .values(expires_at=utcnow()))
