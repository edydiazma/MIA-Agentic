"""Interfaz común de los adaptadores de CRM y capa HTTP (reemplazable en pruebas)."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

import httpx

PROVIDERS = {"hubspot": "HubSpot", "salesforce": "Salesforce"}


class CRMError(Exception):
    """Error del CRM. `retryable` = vale la pena reintentar (red, límite de tasa, 5xx)."""

    def __init__(self, message: str, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class TokenRevoked(CRMError):
    def __init__(self, message: str = "El CRM rechazó las credenciales (token revocado o vencido)"):
        super().__init__(message, retryable=False, status=401)


@dataclass
class RemoteRecord:
    id: str
    properties: dict = field(default_factory=dict)
    updated_at: datetime | None = None


async def send(method: str, url: str, headers: dict | None = None, json_body=None, data=None,
               params=None, timeout: float = 30) -> httpx.Response:
    """Única salida HTTP de los adaptadores (las pruebas la reemplazan)."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        return await http.request(method, url, headers=headers, json=json_body, data=data, params=params)


def check(r: httpx.Response) -> httpx.Response:
    if r.status_code == 401:
        raise TokenRevoked()
    if r.status_code == 429 or r.status_code >= 500:
        raise CRMError(f"HTTP {r.status_code}: {r.text[:300]}", retryable=True, status=r.status_code)
    if r.status_code >= 400:
        raise CRMError(f"HTTP {r.status_code}: {r.text[:500]}", retryable=False, status=r.status_code)
    return r


def _norm_value(v) -> str:
    """Iguala formatos entre sistemas: 89900000.0 == "89900000", fechas con o sin hora."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    text = str(v).strip()
    try:
        f = float(text)
        if f.is_integer() and "e" not in text.lower():
            return str(int(f))
        return repr(f)
    except ValueError:
        pass
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-" and text[10:11] in ("", "T", " "):
        return text[:10] if text[10:].strip("T0:Z.+ ") == "" else text
    return text


def payload_hash(props: dict) -> str:
    """Huella estable de lo enviado/recibido: evita reenviar lo mismo y el eco en sincronización bidireccional."""
    norm = {k: _norm_value(v) for k, v in props.items()}
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CRMAdapter:
    """Contrato de cada proveedor. Los ids remotos son texto."""

    provider: str = ""

    async def find_contact(self, email: str | None, phone: str | None) -> str | None:
        raise NotImplementedError

    async def upsert_contact(self, remote_id: str | None, props: dict) -> str:
        raise NotImplementedError

    async def upsert_deal(self, remote_id: str | None, props: dict, contact_remote_id: str | None) -> str:
        raise NotImplementedError

    async def add_note(self, contact_remote_id: str, text: str, deal_remote_id: str | None = None) -> str:
        raise NotImplementedError

    async def changed_contacts(self, since: datetime, properties: list[str]) -> list[RemoteRecord]:
        raise NotImplementedError

    async def changed_deals(self, since: datetime, properties: list[str]) -> list[RemoteRecord]:
        raise NotImplementedError
