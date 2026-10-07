"""Token de Supabase Realtime para el panel (§19.2).

GET /api/realtime/token → si REALTIME_TRANSPORT es supabase|both y hay SUPABASE_URL, clave publicable y
SUPABASE_JWT_SECRET: JWT HS256 corto (role authenticated, sub = agents.realtime_subject, app_metadata.org_id) y
los temas a los que el asesor se suscribe. Si no, {"enabled": false} y el panel usa el WebSocket /ws.

Proyectos de Supabase solo con claves asimétricas (sin secreto JWT "legacy"): este token no se puede firmar en
el backend; alternativas: iniciar sesión con Supabase Auth (el token del usuario ya sirve, con auth_user_id
vinculado al asesor) o configurar el backend como proveedor de auth de terceros (Third-Party Auth) con su JWKS.
"""

import uuid
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent
from app.config import get_settings
from app.db import get_session
from app.models import Agent, utcnow

router = APIRouter(prefix="/api/realtime", tags=["realtime"])


def realtime_enabled() -> bool:
    s = get_settings()
    return (s.realtime_transport in ("supabase", "both") and bool(s.supabase_url)
            and bool(s.supabase_publishable_key) and bool(s.supabase_jwt_secret))


def mint(agent: Agent, subject: str, now=None) -> tuple[str, object]:
    s = get_settings()
    now = now or utcnow()
    exp = now + timedelta(minutes=max(5, s.realtime_token_minutes))
    claims = {"aud": "authenticated", "role": "authenticated", "sub": subject,
              "iss": f"{s.supabase_url.rstrip('/')}/auth/v1", "iat": int(now.timestamp()), "exp": int(exp.timestamp()),
              "app_metadata": {"org_id": str(agent.organization_id)}, "agent_id": agent.id}
    return jwt.encode(claims, s.supabase_jwt_secret, algorithm="HS256"), exp


@router.get("/token")
async def realtime_token(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if not realtime_enabled():
        return {"enabled": False}
    me = await session.get(Agent, agent.id)  # en esta sesión, para guardar el sujeto si es nuevo
    if not me.realtime_subject:
        me.realtime_subject = str(uuid.uuid4())
        await session.commit()
    token, exp = mint(me, me.realtime_subject)
    s = get_settings()
    return {"enabled": True, "url": s.supabase_url, "key": s.supabase_publishable_key, "token": token,
            "expires_at": exp, "topics": {"events": f"org:{agent.organization_id}:events",
                                          "agent": f"org:{agent.organization_id}:agent:{agent.id}"}}
