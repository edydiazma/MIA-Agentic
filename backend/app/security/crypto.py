"""Utilidades criptográficas: tokens aleatorios, hash de tokens, comparación en tiempo constante y sobres cifrados
(estado de SSO y códigos de un solo uso) con una llave derivada de JWT_SECRET."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def equal(a: str | None, b: str | None) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _fernet() -> Fernet:
    key = hashlib.sha256(("sso-state:" + get_settings().jwt_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(data: dict, ttl_s: int) -> str:
    """Sobre cifrado y autenticado (no legible ni alterable por el navegador) con vencimiento."""
    payload = {**data, "_exp": int(time.time()) + ttl_s}
    return _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()


def unseal(token: str) -> dict | None:
    try:
        data = json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError, TypeError):
        return None
    if int(data.get("_exp", 0)) < int(time.time()):
        return None
    return data
