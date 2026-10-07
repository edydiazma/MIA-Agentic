"""TOTP (RFC 6238, HMAC-SHA1, 6 dígitos, 30 s) compatible con Google Authenticator, Microsoft Authenticator, Authy."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def now_code(secret: str, at: float | None = None) -> str:
    return code_at(secret, int((at or time.time()) // STEP))


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Acepta el código del paso actual ±window (desfase de reloj). Comparación en tiempo constante."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return False
    counter = int((at or time.time()) // STEP)
    ok = False
    for delta in range(-window, window + 1):
        ok |= hmac.compare_digest(code_at(secret, counter + delta), code)
    return ok


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")
