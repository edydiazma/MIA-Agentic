import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.auth import agent_from_token, hash_password
from app.config import get_settings
from app.db import SessionLocal, check_schema
from app.models import Agent, Channel
from app.routers.platform import ensure_first_admin
from app.tenancy import count_orgs, provision_ai_defaults
from app.realtime import hub

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

ROUTERS = ["webhook", "auth", "inbox", "bots", "knowledge", "contacts", "campaigns", "automation", "agenda", "config",
           "reports", "classifier", "cortex", "memory", "seller", "catalog", "json_edit", "flows",
           # Fase 2
           "tracking", "attribution", "conversions", "deals", "integrations", "calls", "voice", "signup",
           "billing", "platform",
           # Mensajes disparadores
           "wa_links"]


async def bootstrap(org: int | None = None) -> None:
    """Instalación de una sola empresa: admin desde ADMIN_EMAIL, IA del servidor, agente y canal del .env.

    En modo SaaS cada empresa nace en /api/signup (tenancy.create_org + provision_ai_defaults)."""
    org = org or settings.organization_id
    async with SessionLocal() as session:
        if not await session.scalar(select(Agent.id).where(Agent.organization_id == org).limit(1)):
            session.add(Agent(organization_id=org, email=settings.admin_email.lower(), name="Administrador",
                              password_hash=hash_password(settings.admin_password), role="admin"))
        agent = await provision_ai_defaults(session, org)
        if settings.wa_phone_number_id and not await session.scalar(
                select(Channel.id).where(Channel.phone_number_id == settings.wa_phone_number_id)):
            session.add(Channel(organization_id=org, name="WhatsApp principal",
                                phone_number_id=settings.wa_phone_number_id, waba_id=settings.wa_waba_id or None,
                                default_ai_agent_id=agent.id))
        await session.commit()


async def startup_bootstrap() -> None:
    """Solo para instalaciones de una empresa (ADMIN_EMAIL definido) y mientras no haya otras empresas."""
    async with SessionLocal() as session:
        run = "admin_email" in settings.model_fields_set and await count_orgs(session) <= 1
        await ensure_first_admin(session)
    if run:
        await bootstrap()


async def reporting_loop() -> None:
    """Sin pg_cron (Postgres local): refresca los rollups cada 10 minutos desde el backend."""
    while True:
        try:
            async with SessionLocal() as session:
                await session.execute(text("select reporting.refresh_recent()"))
                await session.commit()
        except Exception:
            log.exception("Falló el refresco de reportes")
        await asyncio.sleep(600)


async def _has_pg_cron() -> bool:
    async with SessionLocal() as session:
        return bool(await session.scalar(text("select exists(select 1 from pg_extension where extname = 'pg_cron')")))


# Tareas de fondo: "módulo:función". Todas recorren todas las empresas (nunca una empresa fija);
# un módulo que todavía no existe se omite con una advertencia.
LOOPS = [
    "app.automations:inactivity_loop",
    "app.flows.engine:resume_loop",
    "app.catalog:feed_loop",
    "app.plans:usage_loop",
    "app.conversions:conversions_loop",  # detecta conversiones y las sube a Google Ads / Meta CAPI
    "app.crm.sync:crm_loop",  # sincroniza HubSpot / Salesforce (outbox + pull)
    "app.ad_enrichment:enrichment_loop",  # nombres de campaña/anuncio (Meta) y de clic (Google Ads)
]


def _load_loop(path: str):
    module, _, func = path.partition(":")
    try:
        return getattr(importlib.import_module(module), func)
    except (ModuleNotFoundError, AttributeError) as e:
        log.warning("Tarea de fondo %s no disponible: %s", path, e)
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.jwt_secret == "change-me":
        log.warning("JWT_SECRET tiene el valor por defecto: cámbialo antes de exponer el servidor")
    await check_schema()
    await startup_bootstrap()
    tasks = [asyncio.create_task(fn()) for fn in (_load_loop(p) for p in LOOPS) if fn]
    if not await _has_pg_cron():
        tasks.append(asyncio.create_task(reporting_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="WA Agent Platform", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
for _name in ROUTERS:
    try:
        app.include_router(importlib.import_module(f"app.routers.{_name}").router)
    except ModuleNotFoundError as e:
        if e.name != f"app.routers.{_name}":
            raise
        log.warning("Router app.routers.%s no disponible todavía", _name)


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket, token: str):
    async with SessionLocal() as session:
        try:
            agent = await agent_from_token(token, session)
        except HTTPException:
            await websocket.close(code=4401)
            return
    await hub.connect(websocket, agent.id, agent.organization_id)
    try:
        while True:
            await websocket.receive_text()  # ping del cliente
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
