"""Diagnóstico de integraciones (preflight): verificaciones de SOLO LECTURA contra los servicios reales.

Reglas (docs/data-model.md §20):
- nunca envía mensajes, no crea plantillas/campañas/cobros; lo único que escribe son objetos temporales que borra
  enseguida (1 byte en Storage bajo `_preflight/`, un secreto temporal en Vault);
- timeouts cortos; cada verificación se ejecuta aislada (una falla no tumba las demás);
- los secretos nunca se devuelven ni se registran: `mask()` deja solo los últimos 4 caracteres;
- cada resultado trae un `detail` en español con el arreglo concreto (variable, permiso, pantalla).

Alcance: `platform` (variables del servidor; organization_id null) u `org` (conexiones de una empresa).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import SystemCheckRun, utcnow

log = logging.getLogger(__name__)

AREAS = ("infra", "supabase", "meta", "google_ads", "hubspot", "salesforce", "stripe", "email", "sso", "push",
         "voice", "ai")
AREA_LABELS = {"infra": "Infraestructura", "supabase": "Supabase", "meta": "Meta (WhatsApp, Messenger, Instagram, Ads)",
               "google_ads": "Google Ads", "hubspot": "HubSpot", "salesforce": "Salesforce", "stripe": "Stripe",
               "email": "Correo (SMTP / IMAP)", "sso": "Inicio de sesión único (SSO)", "push": "Notificaciones push",
               "voice": "Voz", "ai": "Proveedores de IA"}
STATUSES = ("pass", "warn", "fail", "skipped")
HTTP_TIMEOUT = 8.0
CHECK_TIMEOUT = 20.0


def mask(value: str | None) -> str | None:
    """Solo los últimos 4 caracteres: '••••abcd'."""
    if not value:
        return None
    v = str(value)
    return "••••" + v[-4:] if len(v) > 4 else "••••"


@dataclass
class Result:
    area: str
    check_key: str
    label: str
    status: str
    detail: str | None = None
    data: dict = field(default_factory=dict)
    latency_ms: int | None = None

    def out(self) -> dict:
        return {"area": self.area, "check_key": self.check_key, "label": self.label, "status": self.status,
                "detail": self.detail, "data": self.data, "latency_ms": self.latency_ms}


def http_client(timeout: float = HTTP_TIMEOUT) -> httpx.AsyncClient:
    """Cliente HTTP de las verificaciones (las pruebas lo reemplazan)."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


@dataclass
class Context:
    organization_id: int | None
    with_llm: bool = False
    settings: object = field(default_factory=get_settings)

    @asynccontextmanager
    async def db(self) -> AsyncIterator[AsyncSession]:
        """Sesión propia por verificación: corren en paralelo y una AsyncSession no se comparte."""
        async with SessionLocal() as session:
            yield session

    def http(self, timeout: float = HTTP_TIMEOUT) -> httpx.AsyncClient:
        from app.preflight import core  # se resuelve en cada llamada: las pruebas parchean core.http_client

        return core.http_client(timeout)


CheckFn = Callable[[Context], Awaitable[list[Result]]]


@dataclass
class Check:
    area: str
    key: str
    scope: str  # platform | org
    fn: CheckFn


REGISTRY: list[Check] = []


def check(area: str, key: str, scope: str = "platform"):
    """Registra una verificación. Devuelve uno o varios Result (p. ej. uno por canal)."""
    assert area in AREAS and scope in ("platform", "org")

    def deco(fn: CheckFn) -> CheckFn:
        REGISTRY.append(Check(area, key, scope, fn))
        return fn
    return deco


def skipped(area: str, key: str, label: str, why: str) -> list[Result]:
    return [Result(area, key, label, "skipped", why)]


async def timed(coro: Awaitable) -> tuple[object, int]:
    t0 = time.monotonic()
    out = await coro
    return out, int((time.monotonic() - t0) * 1000)


def error_text(r: httpx.Response) -> str:
    """Mensaje de error de una API sin secretos (recortado)."""
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error_user_msg") or str(err)
    elif isinstance(err, str):
        msg = body.get("error_description") or body.get("message") or err
    else:
        msg = (body.get("message") if isinstance(body, dict) else None) or str(body)
    return f"HTTP {r.status_code}: {str(msg)[:240]}"


def _load_checks() -> None:
    # Los módulos registran sus verificaciones al importarse
    from app.preflight import (  # noqa: F401
        checks_ai,
        checks_crm,
        checks_email,
        checks_infra,
        checks_meta,
        checks_misc,
        checks_stripe,
    )


def selected(scope: str, areas: list[str] | None) -> list[Check]:
    _load_checks()
    want = set(areas or AREAS)
    return [c for c in REGISTRY if c.scope == scope and c.area in want]


async def _run_one(ctx: Context, c: Check) -> list[Result]:
    t0 = time.monotonic()
    try:
        results = await asyncio.wait_for(c.fn(ctx), CHECK_TIMEOUT)
    except TimeoutError:
        results = [Result(c.area, c.key, c.key, "fail",
                          f"La verificación tardó más de {int(CHECK_TIMEOUT)} s: revisa la conectividad de salida "
                          "del servidor hacia ese servicio (firewall / security group / DNS).")]
    except Exception as e:  # noqa: BLE001 — una verificación rota no tumba el diagnóstico
        log.exception("Verificación %s falló", c.key)
        results = [Result(c.area, c.key, c.key, "fail", f"Error inesperado al verificar: {type(e).__name__}")]
    ms = int((time.monotonic() - t0) * 1000)
    for r in results:
        if r.latency_ms is None:
            r.latency_ms = ms
    return results


async def execute(ctx: Context, scope: str, areas: list[str] | None = None) -> list[Result]:
    checks = selected(scope, areas)
    batches = await asyncio.gather(*(_run_one(ctx, c) for c in checks))
    return [r for batch in batches for r in batch]


def counts(results: list[Result]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def exit_code(results: list[Result]) -> int:
    """0 = todo bien (o omitido), 1 = advertencias, 2 = fallas (para puertas de despliegue / CI)."""
    c = counts(results)
    return 2 if c["fail"] else 1 if c["warn"] else 0


# --- Persistencia ----------------------------------------------------------------------------------------
async def start_run(session: AsyncSession, org: int | None, trigger: str, agent_id: int | None) -> SystemCheckRun:
    run = SystemCheckRun(organization_id=org, trigger=trigger, started_by=agent_id,
                         app_version=get_settings().app_version)
    session.add(run)
    await session.commit()
    return run


async def save(session: AsyncSession, run: SystemCheckRun, results: list[Result], scope_areas: list[str]) -> None:
    """Guarda el último resultado por verificación y borra los que ya no aplican (p. ej. un canal eliminado)."""
    org = run.organization_id
    keys = [r.check_key for r in results]
    await session.execute(text("""
        delete from public.system_checks
        where coalesce(organization_id, 0) = coalesce(cast(:o as bigint), 0) and area = any(:areas)
          and not (check_key = any(:keys))"""), {"o": org, "areas": scope_areas, "keys": keys})
    for r in results:
        await session.execute(text("""
            insert into public.system_checks (organization_id, area, check_key, label, status, detail, data,
                                              latency_ms, run_id, checked_at)
            values (:o, :area, :key, :label, :status, :detail, cast(:data as jsonb), :ms, :run, now())
            on conflict (coalesce(organization_id, 0), check_key) do update set
              area = excluded.area, label = excluded.label, status = excluded.status, detail = excluded.detail,
              data = excluded.data, latency_ms = excluded.latency_ms, run_id = excluded.run_id,
              checked_at = excluded.checked_at"""),
            {"o": org, "area": r.area, "key": r.check_key, "label": r.label, "status": r.status, "detail": r.detail,
             "data": _json(r.data), "ms": r.latency_ms, "run": run.id})
    c = counts(results)
    run.passed, run.warned, run.failed, run.skipped = c["pass"], c["warn"], c["fail"], c["skipped"]
    run.finished_at = utcnow()
    await session.commit()


def _json(data: dict) -> str:
    import json

    return json.dumps(data or {}, default=str)


async def run_and_save(org: int | None, scope: str, areas: list[str] | None = None, trigger: str = "manual",
                       agent_id: int | None = None, with_llm: bool = False,
                       run: SystemCheckRun | None = None) -> tuple[SystemCheckRun, list[Result]]:
    async with SessionLocal() as session:
        if run is None:
            run = await start_run(session, org, trigger, agent_id)
        else:
            run = await session.merge(run)
        results = await execute(Context(organization_id=org, with_llm=with_llm), scope, areas)
        await save(session, run, results, list(areas or AREAS))
        return run, results
