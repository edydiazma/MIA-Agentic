"""Salida HTTP del hub (las pruebas la reemplazan) y errores comunes."""

import httpx


class HubError(Exception):
    """Error de un servicio externo. `retryable` = vale la pena reintentar (red, 429, 5xx)."""

    def __init__(self, message: str, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


async def send(method: str, url: str, headers: dict | None = None, json_body=None, data=None, params=None,
               content: bytes | None = None, timeout: float = 30) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as http:
        return await http.request(method, url, headers=headers, json=json_body, data=data, params=params,
                                  content=content)


def check(r: httpx.Response, what: str = "") -> httpx.Response:
    if r.status_code in (401, 403):
        raise HubError(f"{what} rechazó las credenciales (HTTP {r.status_code})", retryable=False, status=r.status_code)
    if r.status_code == 429 or r.status_code >= 500:
        raise HubError(f"{what} HTTP {r.status_code}: {r.text[:300]}", retryable=True, status=r.status_code)
    if r.status_code >= 400:
        raise HubError(f"{what} HTTP {r.status_code}: {r.text[:500]}", retryable=False, status=r.status_code)
    return r


def mask(value: str | None) -> str:
    """Secretos en respuestas y registros: solo los últimos 4 caracteres."""
    v = str(value or "")
    return ("•" * 6 + v[-4:]) if len(v) > 4 else ("•" * len(v))
