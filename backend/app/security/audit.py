"""Auditoría de autenticación (auth_events). Se escribe en su propia transacción: un intento fallido queda
registrado aunque la petición termine en error."""

import logging

from fastapi import Request

from app.db import SessionLocal
from app.models import AuthEvent

log = logging.getLogger(__name__)


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:100]
    return request.client.host if request.client else None


def user_agent(request: Request | None) -> str | None:
    return (request.headers.get("user-agent") or "")[:400] or None if request is not None else None


async def record(event: str, request: Request | None = None, *, organization_id: int | None = None,
                 agent_id: int | None = None, email: str | None = None, **detail) -> None:
    try:
        async with SessionLocal() as s:
            s.add(AuthEvent(organization_id=organization_id, agent_id=agent_id, email=(email or None) and email[:200],
                            event=event, ip=client_ip(request), user_agent=user_agent(request),
                            detail={k: v for k, v in detail.items() if v is not None}))
            await s.commit()
    except Exception:
        log.exception("No se pudo registrar el evento de autenticación %s", event)
