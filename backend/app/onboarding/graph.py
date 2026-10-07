"""Cliente mínimo de la Graph API de Meta para el onboarding.

Todo pasa por `call()` (las pruebas lo reemplazan con un falso). Los errores de Meta se convierten en GraphError
con el mensaje y el código de Meta para mostrarlos tal cual en el asistente.
"""

import httpx

from app.config import get_settings


class GraphError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data or {}


def url(path: str) -> str:
    return f"https://graph.facebook.com/{get_settings().wa_api_version}/{path.lstrip('/')}"


async def call(method: str, path: str, token: str | None = None, params: dict | None = None,
               json: dict | None = None, data: dict | bytes | None = None, headers: dict | None = None) -> dict:
    h = dict(headers or {})
    if token:
        h.setdefault("Authorization", f"Bearer {token}")
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(method, url(path), params=params, json=json, content=data if isinstance(data, bytes)
                               else None, data=data if isinstance(data, dict) else None, headers=h)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text[:500]}
    if r.status_code >= 400:
        err = body.get("error") or {}
        raise GraphError(err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}",
                         err.get("code"), r.status_code, body)
    return body
