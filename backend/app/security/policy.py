"""Política de contraseñas, IPs permitidas, bloqueo y vencimiento (org_settings key 'security')."""

import hashlib
import ipaddress
import logging
import re
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, verify_password
from app.models import Agent, PasswordHistory, utcnow
from app.settings_store import DEFAULTS, get_setting

log = logging.getLogger(__name__)
MAX_LENGTH = 128
COMMON = {"password", "contraseña", "12345678", "123456789", "1234567890", "qwerty123", "admin123", "colombia",
          "iloveyou", "welcome1", "password1", "abc12345", "11111111", "00000000"}


async def get_policy(session: AsyncSession, org: int) -> dict:
    return await get_setting(session, "security", org)


def sanitize(value: dict) -> dict:
    """Valida y normaliza la política que envía el panel (lanza ValueError con el problema)."""
    base = DEFAULTS["security"]
    out: dict = {}
    for k, default in base.items():
        if k not in value:
            continue
        v = value[k]
        if isinstance(default, bool):
            out[k] = bool(v)
        elif isinstance(default, int):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"«{k}» debe ser un número") from None
        elif k == "allowed_ips":
            nets = []
            for raw in v or []:
                raw = str(raw).strip()
                if not raw:
                    continue
                try:
                    nets.append(str(ipaddress.ip_network(raw, strict=False)))
                except ValueError:
                    raise ValueError(f"IP o rango inválido: {raw}") from None
            out[k] = nets
        elif k == "mfa_required":
            if v not in ("none", "admins", "all"):
                raise ValueError("mfa_required debe ser none, admins o all")
            out[k] = v
        else:
            out[k] = v
    ranges = {"min_length": (8, 128), "expiry_days": (0, 730), "history": (0, 24), "lockout_attempts": (3, 20),
              "lockout_minutes": (1, 1440), "trusted_device_days": (0, 90), "session_timeout_minutes": (15, 43200)}
    for k, (lo, hi) in ranges.items():
        if k in out and not lo <= out[k] <= hi:
            raise ValueError(f"«{k}» debe estar entre {lo} y {hi}")
    return out


def ip_allowed(ip: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n, strict=False) for n in allowed)


def strength_errors(policy: dict, password: str, email: str = "", name: str = "") -> list[str]:
    errors = []
    if len(password) < int(policy.get("min_length", 10)):
        errors.append(f"Debe tener al menos {policy.get('min_length', 10)} caracteres")
    if len(password) > MAX_LENGTH:
        errors.append(f"Debe tener máximo {MAX_LENGTH} caracteres")
    if policy.get("require_upper") and not re.search(r"[A-ZÁÉÍÓÚÑ]", password):
        errors.append("Debe incluir una mayúscula")
    if policy.get("require_lower") and not re.search(r"[a-záéíóúñ]", password):
        errors.append("Debe incluir una minúscula")
    if policy.get("require_digit") and not re.search(r"\d", password):
        errors.append("Debe incluir un número")
    if policy.get("require_symbol") and not re.search(r"[^\w\s]", password):
        errors.append("Debe incluir un símbolo")
    low = password.lower()
    if low in COMMON:
        errors.append("Es una contraseña demasiado común")
    local = (email or "").split("@")[0].lower()
    if len(local) >= 4 and local in low:
        errors.append("No debe contener tu correo")
    for part in (name or "").lower().split():
        if len(part) >= 4 and part in low:
            errors.append("No debe contener tu nombre")
            break
    return errors


async def breached(password: str) -> bool:
    """k-anonimato: solo se envían los 5 primeros caracteres del SHA-1. Si el servicio falla, no bloquea."""
    digest = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 (lo exige la API de HIBP)
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"https://api.pwnedpasswords.com/range/{digest[:5]}",
                               headers={"Add-Padding": "true"})
        return any(line.split(":")[0] == digest[5:] for line in r.text.splitlines())
    except httpx.HTTPError:
        log.warning("No se pudo consultar Have I Been Pwned")
        return False


async def validate_new_password(session: AsyncSession, agent: Agent, password: str, policy: dict | None = None) -> list[str]:
    policy = policy or await get_policy(session, agent.organization_id)
    errors = strength_errors(policy, password, agent.email, agent.name)
    if errors:
        return errors
    n = int(policy.get("history", 5))
    if n:
        previous = [agent.password_hash] + list((await session.scalars(
            select(PasswordHistory.password_hash).where(PasswordHistory.agent_id == agent.id)
            .order_by(PasswordHistory.created_at.desc()).limit(n))).all())
        if any(verify_password(password, h) for h in previous if h):
            return [f"No puedes repetir tus últimas {n} contraseñas"]
    if policy.get("breached_check") and await breached(password):
        return ["Esta contraseña apareció en filtraciones públicas: elige otra"]
    return []


async def set_password(session: AsyncSession, agent: Agent, password: str, *, must_change: bool = False) -> None:
    """Guarda la contraseña (ya validada) y su historial; desbloquea la cuenta."""
    if agent.password_hash:
        session.add(PasswordHistory(agent_id=agent.id, password_hash=agent.password_hash))
    agent.password_hash = hash_password(password)
    agent.password_changed_at = utcnow()
    agent.must_change_password = must_change
    agent.failed_logins = 0
    agent.locked_until = None


def password_expired(agent: Agent, policy: dict) -> bool:
    days = int(policy.get("expiry_days") or 0)
    if not days or not agent.password_hash:
        return False
    changed = agent.password_changed_at or agent.created_at
    return changed is not None and changed + timedelta(days=days) < utcnow()


def mfa_required(agent: Agent, policy: dict) -> bool:
    mode = policy.get("mfa_required", "none")
    return mode == "all" or (mode == "admins" and agent.role == "admin")
