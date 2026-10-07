"""Conexión de canales Messenger, Instagram y chat web (docs/data-model.md §12.2).

Messenger e Instagram: página de Facebook + token de página (Vault) y suscripción de la app a la página.
Instagram necesita además el id de la cuenta profesional vinculada (los webhooks llegan con ese id).
Chat web: genera la llave pública del widget (/w/{key}.js) y guarda su apariencia y dominios permitidos.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.channels import ChannelError
from app.channels.meta import subscribe_page
from app.config import get_settings
from app.db import get_session
from app.models import AIAgent, Agent, Channel, ContactIdentity, Conversation
from app.plans import enforce_limit, has_feature
from app.routers.config import _ch, _check_bot
from app.secrets_vault import get_secret, put_secret

router = APIRouter(prefix="/api", tags=["omnichannel"])
env = get_settings()

WEBCHAT_DEFAULTS = {"title": "¿Te ayudamos?", "greeting": "¡Hola! Escríbenos y te respondemos aquí mismo.",
                    "color": "#0f766e", "position": "right", "allowed_domains": [],
                    "prechat": {"enabled": False, "require_email": False}}


class MetaChannelIn(BaseModel):
    provider: str = Field(pattern="^(messenger|instagram)$")
    name: str = Field(min_length=1, max_length=120)
    page_id: str = Field(min_length=1)
    ig_account_id: str | None = None   # solo Instagram: cuenta profesional vinculada a la página
    access_token: str | None = None    # token de página; vacío al editar = conservar
    bot_id: int | None = None
    subscribe: bool = True             # suscribir la app a los eventos de la página


class WebchatIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    bot_id: int | None = None
    settings: dict = {}


async def _require_omnichannel(session: AsyncSession, org: int) -> None:
    if not await has_feature(session, org, "omnichannel"):
        raise HTTPException(402, "Tu plan no incluye «Omnicanal» (Instagram, Messenger y chat web). "
                                 "Actualiza el plan para usarlo.")


async def _default_bot(session: AsyncSession, org: int, bot_id: int | None) -> int | None:
    return await _check_bot(session, org, bot_id) or await session.scalar(
        select(AIAgent.id).where(AIAgent.organization_id == org).order_by(AIAgent.id).limit(1))


async def _get(session: AsyncSession, org: int, cid: int) -> Channel:
    c = await session.get(Channel, cid)
    if not c or c.organization_id != org:
        raise HTTPException(404, "Canal no encontrado")
    return c


def _clean_settings(raw: dict, current: dict | None = None) -> dict:
    out = {**WEBCHAT_DEFAULTS, **(current or {})}
    for k in ("title", "greeting", "color", "position"):
        if isinstance(raw.get(k), str):
            out[k] = raw[k].strip()[:300]
    if isinstance(raw.get("allowed_domains"), list):
        out["allowed_domains"] = sorted({str(d).strip().lower().removeprefix("https://").removeprefix("http://")
                                         .strip("/") for d in raw["allowed_domains"] if str(d).strip()})
    if isinstance(raw.get("prechat"), dict):
        out["prechat"] = {"enabled": bool(raw["prechat"].get("enabled")),
                          "require_email": bool(raw["prechat"].get("require_email"))}
    if not str(out.get("color", "")).startswith("#"):
        out["color"] = WEBCHAT_DEFAULTS["color"]
    return out


@router.post("/channels/meta")
async def create_meta_channel(body: MetaChannelIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    await _require_omnichannel(session, org)
    external = (body.ig_account_id if body.provider == "instagram" else body.page_id or "").strip()
    if not external:
        raise HTTPException(422, "Instagram necesita el id de la cuenta profesional vinculada a la página")
    if await session.scalar(select(Channel.id).where(Channel.provider == body.provider,
                                                     Channel.external_id == external)):
        raise HTTPException(409, "Esa cuenta ya está conectada")
    if not body.access_token:
        raise HTTPException(422, "Falta el token de acceso de la página")
    await enforce_limit(session, org, "channels")
    c = Channel(organization_id=org, name=body.name.strip(), provider=body.provider, external_id=external,
                page_id=body.page_id.strip(), default_ai_agent_id=await _default_bot(session, org, body.bot_id),
                settings={})
    session.add(c)
    await session.flush()
    c.access_token_secret_id = await put_secret(session, body.access_token, f"channel_token:{c.id}")
    if body.subscribe:
        try:
            await subscribe_page(c.page_id, body.access_token)
        except ChannelError as e:
            c.status, c.last_error = "error", str(e)[:1000]
    await session.commit()
    return _ch(c)


@router.put("/channels/{cid}/meta")
async def update_meta_channel(cid: int, body: MetaChannelIn, agent: Agent = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    c = await _get(session, agent.organization_id, cid)
    if c.provider not in ("messenger", "instagram"):
        raise HTTPException(409, "El canal no es de Messenger ni de Instagram")
    c.name = body.name.strip()
    c.page_id = body.page_id.strip()
    if body.bot_id is not None:
        c.default_ai_agent_id = await _check_bot(session, agent.organization_id, body.bot_id)
    if body.access_token:
        c.access_token_secret_id = await put_secret(session, body.access_token, f"channel_token:{c.id}",
                                                    c.access_token_secret_id)
    if body.subscribe:
        token = body.access_token or await get_secret(session, c.access_token_secret_id)
        try:
            await subscribe_page(c.page_id, token or "")
            c.status, c.last_error = "active", None
        except ChannelError as e:
            c.status, c.last_error = "error", str(e)[:1000]
    await session.commit()
    return _ch(c)


@router.post("/channels/webchat")
async def create_webchat(body: WebchatIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    await _require_omnichannel(session, org)
    await enforce_limit(session, org, "channels")
    c = Channel(organization_id=org, name=body.name.strip(), provider="webchat", external_id=secrets.token_urlsafe(16),
                default_ai_agent_id=await _default_bot(session, org, body.bot_id),
                settings=_clean_settings(body.settings))
    session.add(c)
    await session.commit()
    return {**_ch(c), "embed": embed_snippet(c)}


@router.put("/channels/{cid}/webchat")
async def update_webchat(cid: int, body: WebchatIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    c = await _get(session, agent.organization_id, cid)
    if c.provider != "webchat":
        raise HTTPException(409, "El canal no es un chat web")
    c.name = body.name.strip()
    if body.bot_id is not None:
        c.default_ai_agent_id = await _check_bot(session, agent.organization_id, body.bot_id)
    c.settings = _clean_settings(body.settings, c.settings)
    await session.commit()
    return {**_ch(c), "embed": embed_snippet(c)}


@router.get("/channels/{cid}/webchat/embed")
async def webchat_embed(cid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    c = await _get(session, agent.organization_id, cid)
    if c.provider != "webchat":
        raise HTTPException(409, "El canal no es un chat web")
    return {"embed": embed_snippet(c), "script_url": script_url(c)}


@router.delete("/channels/{cid}")
async def delete_channel(cid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Solo canales sin conversaciones (para no perder historial); si tiene, se desconecta."""
    c = await _get(session, agent.organization_id, cid)
    if await session.scalar(select(Conversation.id).where(Conversation.channel_id == c.id).limit(1)):
        c.status = "disconnected"
        await session.commit()
        return {"ok": True, "disconnected": True}
    await session.delete(c)
    await session.commit()
    return {"ok": True, "deleted": True}


def script_url(c: Channel) -> str:
    return f"{env.public_base_url.rstrip('/')}/w/{c.external_id}.js"


def embed_snippet(c: Channel) -> str:
    return f'<script src="{script_url(c)}" async></script>'


@router.get("/contacts/{contact_id}/identities")
async def contact_identities(contact_id: int, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    """Identidades del contacto en cada canal (número, @usuario, Messenger, visitante web)."""
    rows = (await session.execute(
        select(ContactIdentity, Channel.name).outerjoin(Channel, Channel.id == ContactIdentity.channel_id)
        .where(ContactIdentity.contact_id == contact_id,
               ContactIdentity.organization_id == agent.organization_id)
        .order_by(ContactIdentity.created_at))).all()
    return [{"id": i.id, "provider": i.provider, "channel_id": i.channel_id, "channel_name": name,
             "external_id": i.external_id, "username": i.username, "last_inbound_at": i.last_inbound_at,
             "created_at": i.created_at} for i, name in rows]

