import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select, text

from app.auth import agent_from_token, hash_password
from app.config import get_settings
from app.db import SessionLocal, check_schema
from app.models import Agent, Channel
from app.routers.platform import ensure_first_admin
from app.tenancy import count_orgs, provision_ai_defaults
from app.ops import heartbeat, leader, metrics, observability
from app.realtime import hub

settings = get_settings()
observability.setup_logging()
observability.setup_sentry()
log = logging.getLogger(__name__)

ROUTERS = ["webhook", "auth", "inbox", "bots", "knowledge", "contacts", "campaigns", "automation", "agenda", "config",
           "reports", "classifier", "cortex", "memory", "seller", "catalog", "json_edit", "flows",
           # Fase 2
           "tracking", "attribution", "conversions", "deals", "integrations", "calls", "voice", "signup",
           "billing", "platform",
           # Mensajes disparadores
           "wa_links",
           # Fase 4: API pública (llaves) y app del asesor (Web Push)
           "api_keys", "push",
           # Fase 4: calidad (QA), coaching y pruebas de agentes
           "quality", "agent_tests",
           # Fase 4: omnicanal (Messenger, Instagram, chat web)
           "omnichannel", "webchat", "channel_reports", "meta_webhook",
           # Asistente de onboarding y configuración automática
           "onboarding", "invitations",
           # Cliente 360: productos por interacción
           "interaction_products", "customer_reports",
           # Rastreo por anuncio: rendimiento e inversión
           "ads",
           # Agente avanzado: etapas y tipificaciones; webhooks entrantes (panel y público)
           "pipeline_stages", "inbound_webhooks", "hooks_public",
           # Registro maestro: llaves, vehículos, consentimientos, duplicados y organización de campos
           "golden",
           # Supervisión: monitoreo, asignación masiva, transcripción, roles en grupos (§18.2)
           "monitoring",
           # Productividad: notificaciones, notas, línea de tiempo, iniciar conversación, respuestas rápidas (§18.3)
           "notifications", "outreach",
           # Contact center: estados de asesor, horarios por grupo, enrutamiento y dueño del cliente (§18.1)
           "agent_status", "business_hours", "group_routing",
           # Seguridad: 2FA, contraseñas, SSO, roles y permisos, auditoría de acceso (§18.4)
           "security", "sso_public", "roles"]


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
            leader.report_ok("reporting")
        except Exception:
            log.exception("Falló el refresco de reportes")
        await asyncio.sleep(600)


async def ops_cleanup_loop() -> None:
    """Sin pg_cron: limpia realtime_spill, contadores de límites y latidos viejos cada 10 minutos."""
    while True:
        try:
            async with SessionLocal() as session:
                await session.execute(text("select public.ops_cleanup()"))
                await session.commit()
            leader.report_ok("ops_cleanup")
        except Exception:
            log.exception("Falló la limpieza operativa")
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
    "app.quality.hooks:quality_loop",  # revisiones QA de conversaciones cerradas que no pasaron por service.close
    "app.onboarding.hooks:health_checks_loop",  # valida los números (6 h) y plantillas pendientes (10 min)
    "app.ad_enrichment:ads_catalog_loop",  # anuncios de Meta (cruce publicación → anuncio), cada 6 h
    "app.ads.spend:spend_loop",  # inversión por anuncio y día (Meta Insights, Google Ads), cada hora
    "app.golden.hooks:golden_loop",  # documentos pendientes (1 min) y oportunidades por vehículo (6 h)
    "app.recovery:recovery_loop",  # recuperación por inactividad del agente de IA (intentos + fin)
    "app.notifications:followup_reminder_loop",  # avisa seguimientos / llamadas vencidas (una sola vez)
    "app.automations:sla_loop",  # reglas de SLA con temporizador (una vez por ciclo)
    "app.statuses:sessions_loop",  # sesiones sin latido → desconectado
]


def _load_loop(path: str):
    module, _, func = path.partition(":")
    try:
        return getattr(importlib.import_module(module), func)
    except (ModuleNotFoundError, AttributeError) as e:
        log.warning("Tarea de fondo %s no disponible: %s", path, e)
        return None


def _loop_name(path: str) -> str:
    module, _, func = path.partition(":")
    return f"{module.removeprefix('app.')}.{func}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """ROLE=api: HTTP + WebSocket + LISTEN. ROLE=worker: tareas de fondo (una líder por tarea en el clúster).
    ROLE=all: ambos (una sola réplica o desarrollo)."""
    if settings.jwt_secret == "change-me":
        log.warning("JWT_SECRET tiene el valor por defecto: cámbialo antes de exponer el servidor")
    if settings.role not in ("api", "worker", "all"):
        raise RuntimeError(f"ROLE inválido: {settings.role} (api | worker | all)")
    await check_schema()
    if settings.role in ("api", "all"):
        await startup_bootstrap()
        await hub.start()
    tasks = [asyncio.create_task(heartbeat.heartbeat_loop())]
    if settings.role in ("worker", "all"):
        loops = [(_loop_name(p), fn) for p, fn in ((p, _load_loop(p)) for p in LOOPS) if fn]
        if not await _has_pg_cron():
            loops += [("reporting", reporting_loop), ("ops_cleanup", ops_cleanup_loop)]
        tasks += [asyncio.create_task(leader.run_as_leader(name, fn)) for name, fn in loops]
    log.info("Proceso iniciado: role=%s realtime=%s versión=%s", settings.role, hub.mode, settings.app_version)
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await leader.SHARED.close()  # libera los advisory locks: otra réplica toma las tareas
    await hub.stop()
    await heartbeat.remove()


app = FastAPI(title="WA Agent Platform", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.add_middleware(observability.ObservabilityMiddleware)
for _name in ROUTERS:
    try:
        app.include_router(importlib.import_module(f"app.routers.{_name}").router)
    except ModuleNotFoundError as e:
        if e.name != f"app.routers.{_name}":
            raise
        log.warning("Router app.routers.%s no disponible todavía", _name)

# API pública versionada (docs/api.md): /v1, con su propio OpenAPI en /v1/openapi.json y /v1/docs
from app.public_api import api as public_api  # noqa: E402

app.mount("/v1", public_api)


@app.get("/health")
async def health():
    """Liveness: el proceso responde (no toca la base)."""
    return {"ok": True}


@app.get("/health/ready")
async def ready():
    """Readiness: base alcanzable, LISTEN conectado (api) y latido reciente (worker). 503 si algo falla."""
    ok, details = await heartbeat.readiness()
    return JSONResponse({"ok": ok, **details}, status_code=200 if ok else 503)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    if settings.metrics_token and request.headers.get("authorization") != f"Bearer {settings.metrics_token}":
        raise HTTPException(401, "Token de métricas inválido")
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


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
        if agent.id not in hub.online_agent_ids(agent.organization_id):  # cerró la última pestaña
            try:
                from app.statuses import on_ws_disconnect

                on_ws_disconnect(agent.id, agent.organization_id)
            except (ImportError, AttributeError):
                pass
