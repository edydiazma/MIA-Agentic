from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Agent

settings = get_settings()
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(agent: Agent) -> str:
    payload = {
        "sub": str(agent.id),
        "role": agent.role,
        "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def agent_from_token(token: str, session: AsyncSession) -> Agent:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido") from None
    agent = await session.get(Agent, int(payload["sub"]))
    if not agent or not agent.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo")
    return agent


async def current_agent(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    token: str | None = Query(default=None),  # para <img src> / <audio src>
    session: AsyncSession = Depends(get_session),
) -> Agent:
    raw = creds.credentials if creds else token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")
    return await agent_from_token(raw, session)


async def require_admin(agent: Agent = Depends(current_agent)) -> Agent:
    if agent.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo administradores")
    return agent
