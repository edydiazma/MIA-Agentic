"""Configuraciones: empresa, conversaciones, citas, respuestas rápidas, recursos (Storage),
canales (números de WhatsApp; token en Vault), integraciones y alertas."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import current_agent, require_admin
from app.classifier import validate_classifier
from app.config import get_settings
from app.db import get_session
from app.models import AIAgent, Agent, Alert, Channel, IntegrationConnection, QuickReply, Resource, utcnow
from app.schemas import UTCDateTime
from app.secrets_vault import put_secret
from app.settings_store import DEFAULTS, get_setting, set_setting
from app.plans import enforce_limit

router = APIRouter(prefix="/api", tags=["config"])
env = get_settings()

MAX_RESOURCE = 16 * 1024 * 1024

# Catálogo de integraciones. La sincronización real de cada una es una fase posterior.
INTEGRATIONS = {
    "hubspot": {"name": "HubSpot", "category": "CRM",
                "description": "Sincroniza tus contactos de HubSpot y automatiza mensajes, campañas y flujos por WhatsApp."},
    "google_ads": {"name": "Google Ads", "category": "Publicidad",
                   "description": "Rastrea conversiones de tus campañas a partir de las interacciones por WhatsApp."},
    "meta_ads": {"name": "Meta Ads", "category": "Publicidad",
                 "description": "Envía conversiones de Click to WhatsApp a Meta (API de Conversiones)."},
    "salesforce": {"name": "Salesforce", "category": "CRM",
                   "description": "Sincroniza cuentas y oportunidades con tus conversaciones de WhatsApp."},
}


class QuickReplyIn(BaseModel):
    shortcut: str
    text: str


class ChannelIn(BaseModel):
    name: str
    phone_number_id: str
    display_phone: str | None = None
    waba_id: str | None = None
    access_token: str | None = None  # se guarda en Vault; vacío = conservar
    bot_id: int | None = None  # agente de IA por defecto del canal


class AlertOut(BaseModel):
    id: int
    severity: str
    layer: str
    source: str
    title: str
    description: str | None
    ref: str | None
    resolved: bool
    resolved_at: UTCDateTime | None = None
    created_at: UTCDateTime


# --- Settings -------------------------------------------------------------------
@router.get("/settings/{key}")
async def read_setting(key: str, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    if key not in DEFAULTS:
        raise HTTPException(404, "Configuración desconocida")
    return await get_setting(session, key, agent.organization_id)


@router.put("/settings/{key}")
async def write_setting(key: str, body: dict, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    if key not in DEFAULTS:
        raise HTTPException(404, "Configuración desconocida")
    if key == "company" and body.get("timezone"):
        try:
            ZoneInfo(body["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Zona horaria inválida (usa formato America/Bogota)") from None
    if key == "classifier":
        validate_classifier(body)
    if key == "conversations" and "typifications" in body and not [t for t in body["typifications"] if t.strip()]:
        raise HTTPException(422, "Debe haber al menos una tipificación")
    return await set_setting(session, key, body, agent.organization_id, agent_id=agent.id)


# --- Respuestas rápidas ---------------------------------------------------------
def _qr(q: QuickReply) -> dict:
    return {"id": q.id, "shortcut": q.shortcut, "text": q.text}


def _shortcut(raw: str) -> str:
    s = raw.strip().lstrip("/").lower()
    if not s or len(s) > 50 or not all(c.isalnum() or c in "_-" for c in s):
        raise HTTPException(422, "El atajo solo admite letras, números, _ y -")
    return s


async def _quick(session: AsyncSession, qid: int, agent: Agent) -> QuickReply:
    q = await session.get(QuickReply, qid)
    if not q or q.organization_id != agent.organization_id:
        raise HTTPException(404, "Respuesta rápida no encontrada")
    return q


@router.get("/quick-replies")
async def list_quick_replies(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(QuickReply).where(QuickReply.organization_id == agent.organization_id)
                                  .order_by(QuickReply.shortcut))).all()
    return [_qr(q) for q in rows]


@router.post("/quick-replies")
async def create_quick_reply(body: QuickReplyIn, agent: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    shortcut = _shortcut(body.shortcut)
    if not body.text.strip():
        raise HTTPException(422, "El texto es obligatorio")
    if await session.scalar(select(QuickReply.id).where(QuickReply.organization_id == agent.organization_id,
                                                        QuickReply.shortcut == shortcut)):
        raise HTTPException(409, "Ese atajo ya existe")
    q = QuickReply(organization_id=agent.organization_id, shortcut=shortcut, text=body.text.strip())
    session.add(q)
    await session.commit()
    return _qr(q)


@router.put("/quick-replies/{qid}")
async def update_quick_reply(qid: int, body: QuickReplyIn, agent: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    q = await _quick(session, qid, agent)
    q.shortcut, q.text = _shortcut(body.shortcut), body.text.strip()
    await session.commit()
    return _qr(q)


@router.delete("/quick-replies/{qid}")
async def delete_quick_reply(qid: int, agent: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    await session.delete(await _quick(session, qid, agent))
    await session.commit()
    return {"ok": True}


# --- Gestor de recursos (Supabase Storage) --------------------------------------
def _res(r: Resource) -> dict:
    return {"id": r.id, "name": r.name, "mime": r.mime, "size": r.size_bytes, "created_at": r.created_at}


async def _resource(session: AsyncSession, rid: int, agent: Agent) -> Resource:
    r = await session.get(Resource, rid)
    if not r or r.organization_id != agent.organization_id:
        raise HTTPException(404, "Recurso no encontrado")
    return r


@router.get("/resources")
async def list_resources(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Resource).where(Resource.organization_id == agent.organization_id)
                                  .order_by(Resource.name))).all()
    return [_res(r) for r in rows]


@router.post("/resources")
async def upload_resource(file: UploadFile = File(...), agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    data = await file.read()
    if len(data) > MAX_RESOURCE:
        raise HTTPException(413, "Archivo demasiado grande (máx. 16 MB, límite de WhatsApp)")
    mime = file.content_type or "application/octet-stream"
    path = await storage.upload(
        storage.new_path(storage.RESOURCES_BUCKET, agent.organization_id, "library", mime), data, mime)
    r = Resource(organization_id=agent.organization_id, name=file.filename or "archivo", storage_path=path, mime=mime,
                 size_bytes=len(data), created_by=agent.id)
    session.add(r)
    await session.commit()
    return _res(r)


@router.get("/resources/{rid}/file")
async def resource_file(rid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    r = await _resource(session, rid, agent)
    return Response(content=await storage.download(r.storage_path), media_type=r.mime,
                    headers={"Content-Disposition": f'inline; filename="{r.name}"'})


@router.delete("/resources/{rid}")
async def delete_resource(rid: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    r = await _resource(session, rid, agent)
    try:
        await storage.remove(r.storage_path)
    except Exception:  # el archivo puede no existir; el registro se borra igual
        pass
    await session.delete(r)
    await session.commit()
    return {"ok": True}


# --- Plataforma: números de WhatsApp --------------------------------------------
def _ch(c: Channel) -> dict:
    whatsapp = c.provider == "whatsapp_cloud"
    return {
        "id": c.id, "name": c.name, "provider": c.provider, "phone_number_id": c.phone_number_id,
        "display_phone": c.display_phone, "waba_id": c.waba_id, "bot_id": c.default_ai_agent_id,
        "has_own_token": bool(c.access_token_secret_id),
        "token_configured": bool(c.access_token_secret_id or (whatsapp and env.wa_access_token)
                                 or c.provider == "webchat"),
        "external_id": c.external_id, "page_id": c.page_id, "status": c.status, "last_error": c.last_error,
        "settings": c.settings or {},
    }


async def _check_bot(session: AsyncSession, org: int, bot_id: int | None) -> int | None:
    if bot_id is None:
        return None
    a = await session.get(AIAgent, bot_id)
    if not a or a.organization_id != org:
        raise HTTPException(404, "Agente de IA no encontrado")
    return a.id


@router.get("/channels")
async def list_channels(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Channel).where(Channel.organization_id == agent.organization_id)
                                  .order_by(Channel.id))).unique().all()
    return {
        "channels": [_ch(c) for c in rows],
        "waba_id": next((c.waba_id for c in rows if c.waba_id), None) or env.wa_waba_id or None,
        "webhook_path": "/webhooks/whatsapp",
        "app_secret_configured": bool(env.wa_app_secret),
    }


@router.post("/channels")
async def create_channel(body: ChannelIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    pnid = body.phone_number_id.strip()
    if await session.scalar(select(Channel.id).where(Channel.phone_number_id == pnid)):
        raise HTTPException(409, "Ese número ya está registrado")
    await enforce_limit(session, org, "channels")
    bot_id = await _check_bot(session, org, body.bot_id) or await session.scalar(
        select(AIAgent.id).where(AIAgent.organization_id == org).order_by(AIAgent.id).limit(1))
    c = Channel(organization_id=org, name=body.name, phone_number_id=pnid, display_phone=body.display_phone,
                waba_id=body.waba_id or env.wa_waba_id or None, default_ai_agent_id=bot_id)
    session.add(c)
    await session.flush()
    if body.access_token:
        c.access_token_secret_id = await put_secret(session, body.access_token, f"channel_token:{c.id}")
    await session.commit()
    return _ch(c)


@router.put("/channels/{cid}")
async def update_channel(cid: int, body: ChannelIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    c = await session.get(Channel, cid)
    if not c or c.organization_id != agent.organization_id:
        raise HTTPException(404, "Canal no encontrado")
    if c.provider != "whatsapp_cloud":
        raise HTTPException(409, "Este canal no es de WhatsApp: edítalo en /api/channels/{id}/omnichannel")
    c.name, c.display_phone = body.name, body.display_phone
    if body.waba_id is not None:
        c.waba_id = body.waba_id or None
    if body.bot_id is not None:
        c.default_ai_agent_id = await _check_bot(session, agent.organization_id, body.bot_id)
    if body.access_token:  # vacío = conservar el token actual
        c.access_token_secret_id = await put_secret(session, body.access_token, f"channel_token:{c.id}",
                                                    c.access_token_secret_id)
    await session.commit()
    return _ch(c)


# --- Integraciones --------------------------------------------------------------
# Clave del panel -> proveedor en integration_connections y pantalla donde se conecta.
INTEGRATION_PROVIDERS = {"hubspot": ("hubspot", "/configuraciones/integraciones"),
                         "salesforce": ("salesforce", "/configuraciones/integraciones"),
                         "google_ads": ("google_ads", "/configuraciones/conversiones"),
                         "meta_ads": ("meta", "/configuraciones/conversiones")}


async def integration_status(session: AsyncSession, org: int) -> list[dict]:
    rows = {c.provider: c for c in (await session.scalars(
        select(IntegrationConnection).where(IntegrationConnection.organization_id == org))).all()}
    out = []
    for key, meta in INTEGRATIONS.items():
        provider, href = INTEGRATION_PROVIDERS[key]
        c = rows.get(provider)
        out.append({"key": key, **meta, "href": href, "available": True,
                    "connected": bool(c and c.status == "connected"), "status": c.status if c else None,
                    "last_sync_at": c.last_sync_at if c else None, "last_error": c.last_error if c else None})
    return out


@router.get("/integrations")
async def list_integrations(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    return await integration_status(session, agent.organization_id)


# --- Alertas --------------------------------------------------------------------
def _alert(a: Alert) -> AlertOut:
    return AlertOut(id=a.id, severity=a.severity, layer=a.layer, source=a.source, title=a.title,
                    description=a.description, ref=a.ref, resolved=a.resolved_at is not None,
                    resolved_at=a.resolved_at, created_at=a.created_at)


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(resolved: bool = False, limit: int = 100, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    cond = Alert.resolved_at.is_not(None) if resolved else Alert.resolved_at.is_(None)
    rows = (await session.scalars(select(Alert).where(Alert.organization_id == agent.organization_id, cond)
                                  .order_by(Alert.id.desc()).limit(min(limit, 500)))).all()
    return [_alert(a) for a in rows]


@router.post("/alerts/{aid}/resolve")
async def resolve_alert(aid: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    a = await session.get(Alert, aid)
    if not a or a.organization_id != agent.organization_id:
        raise HTTPException(404, "Alerta no encontrada")
    if not a.resolved_at:
        a.resolved_at, a.resolved_by = utcnow(), agent.id
        await session.commit()
    return {"ok": True}
