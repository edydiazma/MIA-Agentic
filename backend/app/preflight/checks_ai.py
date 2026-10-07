"""Proveedores de IA. Por defecto NO se hace ninguna llamada que cueste: se valida el formato de la llave y se
lista los modelos (gratis). Con `with_llm` (opción `--with-llm` de la CLI) se hace una llamada mínima por conexión."""

from sqlalchemy import select

from app.models import AIConnection
from app.preflight.core import Context, Result, check, error_text, mask, skipped
from app.secrets_vault import get_secret

ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models"
OPENAI_MODELS = "https://api.openai.com/v1/models"


async def list_models(ctx: Context, provider: str, key: str, base_url: str | None = None) -> tuple[bool, str, list]:
    """(ok, detalle, modelos) sin costo."""
    async with ctx.http() as http:
        if provider == "anthropic":
            r = await http.get(ANTHROPIC_MODELS, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif provider in ("openai", "openai_compatible"):
            url = f"{base_url.rstrip('/')}/models" if provider == "openai_compatible" and base_url else OPENAI_MODELS
            r = await http.get(url, headers={"Authorization": f"Bearer {key}"})
        else:
            return True, "Proveedor sin endpoint de modelos gratuito: se verifica solo que la llave exista.", []
    if r.status_code >= 400:
        return False, error_text(r), []
    return True, "", [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]


def _format_ok(provider: str, key: str) -> bool:
    if provider == "anthropic":
        return key.startswith("sk-ant-")
    if provider == "openai":
        return key.startswith("sk-")
    return bool(key)


@check("ai", "ai.server_keys")
async def server_keys(ctx: Context) -> list[Result]:
    s = ctx.settings
    out: list[Result] = []
    for provider, key, env_name in (("anthropic", s.anthropic_api_key, "ANTHROPIC_API_KEY"),
                                    ("openai", s.openai_api_key, "OPENAI_API_KEY")):
        if not key:
            continue
        rk, label = f"ai.server_key:{provider}", f"Llave de IA del servidor ({env_name})"
        if not _format_ok(provider, key):
            out.append(Result("ai", rk, label, "fail", f"{env_name} no tiene el formato esperado.", {"key": mask(key)}))
            continue
        ok, detail, models = await list_models(ctx, provider, key)
        if not ok:
            out.append(Result("ai", rk, label, "fail", f"El proveedor rechazó la llave ({detail}).", {"key": mask(key)}))
            continue
        status, msg = "pass", f"Llave válida; {len(models)} modelos disponibles."
        if provider == s.default_provider and models and s.default_model not in models:
            status, msg = "warn", (f"Llave válida, pero DEFAULT_MODEL={s.default_model} no aparece entre los modelos "
                                   "de la cuenta: revisa el nombre del modelo.")
        out.append(Result("ai", rk, label, status, msg, {"key": mask(key), "models": len(models)}))
    return out or skipped("ai", "ai.server_keys", "Llaves de IA del servidor",
                          "Sin ANTHROPIC_API_KEY ni OPENAI_API_KEY: cada empresa usa sus propias conexiones de IA.")


@check("ai", "ai.connections", scope="org")
async def connections(ctx: Context) -> list[Result]:
    async with ctx.db() as s:
        conns = (await s.scalars(select(AIConnection).where(
            AIConnection.organization_id == ctx.organization_id, AIConnection.is_active))).all()
        keys = {c.id: await get_secret(s, c.api_key_secret_id) for c in conns}
    if not conns:
        return skipped("ai", "ai.connections", "Conexiones de IA",
                       "La empresa no tiene conexiones propias: usa las llaves del servidor.")
    s = ctx.settings
    out: list[Result] = []
    for c in conns:
        key = keys[c.id] or {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key}.get(c.provider, "")
        rk, label = f"ai.connection:{c.id}", f"IA «{c.name}» ({c.provider} · {c.model})"
        if not key:
            out.append(Result("ai", rk, label, "fail", "La conexión no tiene llave guardada ni llave del servidor.",
                              {"connection_id": c.id}))
            continue
        ok, detail, models = await list_models(ctx, c.provider, key, c.base_url)
        if not ok:
            out.append(Result("ai", rk, label, "fail", f"El proveedor rechazó la llave ({detail}).",
                              {"connection_id": c.id, "key": mask(key)}))
            continue
        status, msg = "pass", "Llave válida."
        if models and c.model not in models:
            status, msg = "warn", f"La llave es válida pero el modelo «{c.model}» no está disponible en la cuenta."
        if ctx.with_llm:
            status, msg = await _llm_ping(ctx, c, status, msg)
        out.append(Result("ai", rk, label, status, msg, {"connection_id": c.id, "key": mask(key)}))
    return out


async def _llm_ping(ctx: Context, c: AIConnection, status: str, msg: str) -> tuple[str, str]:
    """Llamada mínima a través del mismo camino que usa la app (Cortex temporal de un miembro, propósito test)."""
    try:
        from app.models import Cortex, CortexMember
        from app.routers.cortex import _run_test
    except ImportError:
        return status, msg
    async with ctx.db() as s:
        conn = await s.get(AIConnection, c.id)
        temp = Cortex(name=f"preflight:{conn.name}", organization_id=conn.organization_id, strategy="failover",
                      max_attempts=1, circuit_breaker_failures=10_000, circuit_breaker_cooldown_s=1, validation={},
                      is_active=True)
        temp.members = [CortexMember(connection_id=conn.id, connection=conn, position=1, weight=1)]
        res = await _run_test(s, temp, conn.organization_id, "Responde «ok».")
        await s.rollback()  # el Cortex temporal nunca se guarda
    if res.get("ok"):
        return status, msg + " La llamada de prueba respondió."
    return "fail", f"La llamada de prueba falló: {str(res.get('error'))[:200]}"
