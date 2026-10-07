"""Autenticación de asesores (JWT propio) con aislamiento por empresa.

El token lleva:
- sub: id del asesor (fila de `agents`, que es la membresía en UNA empresa)
- org: empresa de esa membresía (debe coincidir con agents.organization_id)
- orgs: empresas a las que el mismo correo pudo entrar con su contraseña al iniciar sesión (para cambiar de empresa
  sin volver a pedirla; nunca se infiere solo por el correo, que no está verificado)
Los tokens del back-office de la plataforma usan audiencia "platform" y no sirven aquí.
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Agent, Organization

settings = get_settings()
bearer = HTTPBearer(auto_error=False)

# Rutas que una empresa con prueba vencida / cancelada sigue pudiendo usar (para pagar o ver su estado)
BILLING_SAFE_PREFIXES = ("/api/billing", "/api/plan", "/api/auth")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(agent: Agent, orgs: list[int] | None = None) -> str:
    payload = {
        "sub": str(agent.id),
        "org": agent.organization_id,
        "orgs": sorted(set((orgs or []) + [agent.organization_id]))[:50],
        "role": agent.role,
        "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        # Sin `audience`: un token con audiencia (p. ej. el del back-office) se rechaza aquí
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido") from None


async def agent_from_token(token: str, session: AsyncSession, path: str | None = None) -> Agent:
    payload = decode_token(token)
    agent = await session.get(Agent, int(payload["sub"]))
    if not agent or not agent.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo")
    if "org" in payload and int(payload["org"]) != agent.organization_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
    org = await session.get(Organization, agent.organization_id)
    from app.tenancy import org_access_error

    error = org_access_error(org)
    if error:
        # Suspendida (decisión de la plataforma): solo puede ver su sesión y su plan.
        # Prueba vencida o cancelada: además puede pagar (/api/billing).
        allowed = ("/api/auth", "/api/plan") if org and org.status == "suspended" else BILLING_SAFE_PREFIXES
        if not (path or "").startswith(allowed):
            raise HTTPException(error[0], error[1])
    return agent


async def current_agent(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    token: str | None = Query(default=None),  # para <img src> / <audio src>
    session: AsyncSession = Depends(get_session),
) -> Agent:
    raw = creds.credentials if creds else token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")
    agent = await agent_from_token(raw, session, request.url.path)
    request.state.token_orgs = decode_token(raw).get("orgs") or [agent.organization_id]
    return agent


async def require_admin(agent: Agent = Depends(current_agent)) -> Agent:
    if agent.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo administradores")
    return agent
