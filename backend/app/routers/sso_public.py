"""Pasos públicos del inicio de sesión único: descubrir por dominio, ir al proveedor, volver (OIDC / SAML ACS),
metadata del proveedor de servicio y canje del código de un solo uso por el token del panel."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Agent, SSOConnection
from app.ops import ratelimit
from app.security import audit, sso

router = APIRouter(tags=["sso"])


def _frontend(path: str, **params) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}{path}" + (("?" + urlencode(params)) if params else "")


async def _conn(session: AsyncSession, slug: str) -> SSOConnection:
    c = await session.scalar(select(SSOConnection).where(SSOConnection.slug == slug))
    if not c or not c.is_active:
        raise HTTPException(404, "Inicio de sesión único no disponible")
    return c


@router.get("/api/auth/sso/discover")
async def discover(email: str, request: Request, session: AsyncSession = Depends(get_session)):
    if not await ratelimit.allow(session, f"sso_discover:{audit.client_ip(request)}", 60, 60):
        raise HTTPException(429, "Demasiadas consultas")
    c = await sso.discover(session, email)
    return {"sso": {"slug": c.slug, "protocol": c.protocol, "name": c.name, "enforce": c.enforce} if c else None}


@router.get("/auth/sso/{slug}/start")
async def start(slug: str, session: AsyncSession = Depends(get_session)):
    c = await _conn(session, slug)
    try:
        url = await sso.oidc_start(c) if c.protocol == "oidc" else sso.saml_start(c)
    except sso.SSOError as e:
        return RedirectResponse(_frontend("/login", sso_error=str(e)), status_code=302)
    return RedirectResponse(url, status_code=302)


async def _finish(session: AsyncSession, c: SSOConnection, ident: dict, request: Request) -> RedirectResponse:
    try:
        agent = await sso.provision(session, c, ident)
    except sso.SSOError as e:
        await session.rollback()
        await audit.record("sso_failed", request, organization_id=c.organization_id, email=ident.get("email"),
                           reason=str(e))
        return RedirectResponse(_frontend("/login", sso_error=str(e)), status_code=302)
    await session.commit()
    return RedirectResponse(_frontend("/sso/callback", code=sso.login_code(agent)), status_code=302)


@router.get("/auth/sso/{slug}/callback")
async def oidc_callback(slug: str, request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, session: AsyncSession = Depends(get_session)):
    c = await _conn(session, slug)
    if error or not code:
        await audit.record("sso_failed", request, organization_id=c.organization_id, reason=error or "sin código")
        return RedirectResponse(_frontend("/login", sso_error="El proveedor canceló el inicio de sesión"),
                                status_code=302)
    try:
        ident = await sso.oidc_callback(session, c, code, state or "")
    except sso.SSOError as e:
        await audit.record("sso_failed", request, organization_id=c.organization_id, reason=str(e))
        return RedirectResponse(_frontend("/login", sso_error=str(e)), status_code=302)
    return await _finish(session, c, ident, request)


@router.post("/auth/sso/{slug}/acs")
async def saml_acs(slug: str, request: Request, SAMLResponse: str = Form(...), RelayState: str = Form(""),  # noqa: N803
                   session: AsyncSession = Depends(get_session)):
    c = await _conn(session, slug)
    try:
        ident = await sso.saml_acs(c, SAMLResponse, RelayState)
    except sso.SSOError as e:
        await audit.record("sso_failed", request, organization_id=c.organization_id, reason=str(e))
        return RedirectResponse(_frontend("/login", sso_error=str(e)), status_code=303)
    resp = await _finish(session, c, ident, request)
    resp.status_code = 303  # POST → GET
    return resp


@router.get("/auth/sso/{slug}/metadata")
async def metadata(slug: str, session: AsyncSession = Depends(get_session)):
    c = await session.scalar(select(SSOConnection).where(SSOConnection.slug == slug, SSOConnection.protocol == "saml"))
    if not c:
        raise HTTPException(404, "No encontrado")
    return Response(sso.sp_metadata(c), media_type="application/samlmetadata+xml")


class ExchangeIn(BaseModel):
    code: str


@router.post("/api/auth/sso/exchange")
async def exchange(body: ExchangeIn, request: Request, session: AsyncSession = Depends(get_session)):
    agent_id = await sso.redeem_code(body.code)
    agent = await session.get(Agent, agent_id) if agent_id else None
    if not agent or not agent.is_active:
        raise HTTPException(401, "El inicio de sesión venció: vuelve a intentarlo")
    from app.routers.auth import finish_login

    return await finish_login(session, agent, [agent.organization_id], request, method="sso")
