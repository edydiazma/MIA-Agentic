"""Llaves de la API pública: formato, hash y alcances. docs/data-model.md §12.4 y docs/api.md.

Formato: wak_live_<prefijo 8>_<secreto 32>. Solo se guarda el SHA-256 de la llave completa; el prefijo
(wak_live_<prefijo 8>) es visible en el panel para reconocerla.
"""

import hashlib
import re
import secrets
import string

SCOPES = {
    "contacts:read": "Leer contactos",
    "contacts:write": "Crear y editar contactos",
    "conversations:read": "Leer conversaciones",
    "conversations:write": "Asignar, cerrar y tipificar conversaciones",
    "messages:read": "Leer mensajes",
    "messages:send": "Enviar mensajes",
    "deals:read": "Leer negocios",
    "deals:write": "Crear y mover negocios",
    "webhooks:manage": "Suscribir webhooks (Zapier, Make, n8n)",
    "reports:read": "Leer reportes",
    "journeys:write": "Inscribir clientes en journeys y enviar eventos",
}
KEY_RE = re.compile(r"^(wak_live_[a-z0-9]{8})_([A-Za-z0-9]{32})$")
_ALPHABET = string.ascii_lowercase + string.digits
_SECRET_ALPHABET = string.ascii_letters + string.digits


def hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def new_key() -> tuple[str, str, str]:
    """(llave completa, prefijo visible, hash)."""
    prefix = "wak_live_" + "".join(secrets.choice(_ALPHABET) for _ in range(8))
    full = f"{prefix}_{''.join(secrets.choice(_SECRET_ALPHABET) for _ in range(32))}"
    return full, prefix, hash_key(full)


def parse(full_key: str) -> str | None:
    """Prefijo de una llave bien formada; None si el formato no es válido."""
    m = KEY_RE.match(full_key or "")
    return m.group(1) if m else None


def clean_scopes(scopes: list[str]) -> list[str]:
    bad = sorted(set(scopes) - SCOPES.keys())
    if bad:
        raise ValueError(f"Alcances inválidos: {', '.join(bad)}")
    return sorted(set(scopes))
