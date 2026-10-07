import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from app.auth import agent_from_token, hash_password
from app.automations import inactivity_loop
from app.config import get_settings
from app.db import SessionLocal, check_schema
from app.models import Agent, AIAgent, AIConnection, Channel, Cortex, CortexMember
from app.realtime import hub

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

DEFAULT_PROMPT = """Eres el asistente virtual de la empresa. Atiendes a los clientes por WhatsApp en español, \
con un tono cercano y profesional.

## Objetivo
Resolver dudas frecuentes, recopilar los datos necesarios del cliente y transferir a un asesor humano cuando \
haga falta.

## Información del negocio
(Completa aquí: productos, precios, horarios, sedes, políticas, preguntas frecuentes.)
"""

ROUTERS = ["webhook", "auth", "inbox", "bots", "knowledge", "contacts", "campaigns", "automation", "agenda", "config",
           "reports", "classifier", "cortex", "memory", "seller", "catalog", "json_edit", "flows"]


async def bootstrap(org: int | None = None) -> None:
    """Datos mínimos para operar: admin, conexiones de IA del servidor, Cortex principal, agente y canal."""
    org = org or settings.organization_id
    async with SessionLocal() as session:
        if not await session.scalar(select(Agent.id).where(Agent.organization_id == org).limit(1)):
            session.add(Agent(organization_id=org, email=settings.admin_email.lower(), name="Administrador",
                              password_hash=hash_password(settings.admin_password), role="admin"))

        cortex = await session.scalar(select(Cortex).where(Cortex.organization_id == org).order_by(Cortex.id).limit(1))
        if not await session.scalar(select(AIConnection.id).where(AIConnection.organization_id == org).limit(1)):
            conns = []
            if settings.anthropic_api_key:
                conns.append(AIConnection(
                    organization_id=org, name="Claude (servidor)", provider="anthropic", model=settings.default_model,
                    default_params={"effort": settings.default_effort} if settings.default_effort else {}))
            if settings.openai_api_key:
                conns.append(AIConnection(organization_id=org, name="OpenAI (servidor)", provider="openai",
                                          model=os.environ.get("OPENAI_MODEL", "gpt-4.1")))
            session.add_all(conns)
            await session.flush()
            if conns and not cortex:
                cortex = Cortex(organization_id=org, name="Principal", purpose="any", strategy="failover",
                                description="Conexiones del servidor; Claude primero y OpenAI como respaldo")
                session.add(cortex)
                await session.flush()
                for pos, c in enumerate(conns, start=1):
                    session.add(CortexMember(cortex_id=cortex.id, connection_id=c.id, position=pos))

        agent = await session.scalar(select(AIAgent).where(AIAgent.organization_id == org).order_by(AIAgent.id).limit(1))
        if not agent:
            agent = AIAgent(organization_id=org, name="Asistente principal", system_prompt=DEFAULT_PROMPT,
                            cortex_id=cortex.id if cortex else None)
            session.add(agent)
            await session.flush()

        if settings.wa_phone_number_id and not await session.scalar(
                select(Channel.id).where(Channel.phone_number_id == settings.wa_phone_number_id)):
            session.add(Channel(organization_id=org, name="WhatsApp principal",
                                phone_number_id=settings.wa_phone_number_id, waba_id=settings.wa_waba_id or None,
                                default_ai_agent_id=agent.id))
        await session.commit()


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.jwt_secret == "change-me":
        log.warning("JWT_SECRET tiene el valor por defecto: cámbialo antes de exponer el servidor")
    await check_schema()
    await bootstrap()
    from app.flows.engine import resume_loop

    tasks = [asyncio.create_task(inactivity_loop()), asyncio.create_task(resume_loop())]
    try:
        from app.catalog import feed_loop

        tasks.append(asyncio.create_task(feed_loop()))
    except ImportError:
        log.warning("Catálogo sin sincronización automática (app.catalog.feed_loop no disponible)")
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
    await hub.connect(websocket, agent.id)
    try:
        while True:
            await websocket.receive_text()  # ping del cliente
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
