"""Infraestructura y Supabase (alcance plataforma)."""

import asyncio
import os
import shutil
import socket
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text

from app.models import utcnow
from app.preflight.core import Context, Result, check, error_text, skipped

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"
EXPECTED_CRON = ("reporting-refresh", "partitions-ahead", "retention", "ops-cleanup")
BUCKETS = ("conversation-media", "resources")


# --- Base de datos ------------------------------------------------------------------------------------------
@check("infra", "infra.database")
async def database(ctx: Context) -> list[Result]:
    label = "Base de datos (DATABASE_URL)"
    try:
        async with ctx.db() as s:
            version = await s.scalar(text("show server_version"))
            ext = (await s.execute(text("select extname from pg_extension"))).scalars().all()
    except Exception as e:  # noqa: BLE001
        return [Result("infra", "infra.database", label, "fail",
                       f"No se pudo conectar ({type(e).__name__}). Revisa DATABASE_URL: en Supabase usa el «Session "
                       "pooler» (puerto 5432, IPv4) y la contraseña de la base.")]
    major = int(str(version).split(".")[0]) if version else 0
    status = "pass" if major >= 15 else "warn"
    return [Result("infra", "infra.database", label, status,
                   f"Postgres {version}." + ("" if status == "pass" else " Se recomienda Postgres 15 o superior."),
                   {"version": version, "extensions": sorted(ext)})]


@check("infra", "infra.migrations")
async def migrations(ctx: Context) -> list[Result]:
    label = "Migraciones aplicadas"
    if not MIGRATIONS_DIR.is_dir():
        return skipped("infra", "infra.migrations", label,
                       "La carpeta supabase/migrations no está en este servidor (imagen de Docker): verifica con "
                       "`npx supabase migration list --linked` desde tu equipo.")
    local = sorted(p.name.split("_", 1)[0] for p in MIGRATIONS_DIR.glob("*.sql"))
    async with ctx.db() as s:
        exists = await s.scalar(text("select to_regclass('supabase_migrations.schema_migrations') is not null"))
        if not exists:
            return skipped("infra", "infra.migrations", label,
                           "La base no tiene el historial de la CLI de Supabase (supabase_migrations): solo aplica a "
                           "proyectos de Supabase gestionados con `supabase db push`.")
        applied = set((await s.execute(text("select version from supabase_migrations.schema_migrations"))).scalars())
    missing = [v for v in local if v not in applied]
    if missing:
        return [Result("infra", "infra.migrations", label, "fail",
                       f"Faltan {len(missing)} migraciones en la base: ejecuta `npx supabase db push` (desde la raíz "
                       "del repositorio) antes de desplegar.", {"missing": missing[:20], "local": len(local)})]
    return [Result("infra", "infra.migrations", label, "pass", f"Las {len(local)} migraciones están aplicadas.",
                   {"local": len(local)})]


async def _listen_roundtrip() -> bool:
    from app.ops.pg import connect

    channel = f"preflight_{uuid.uuid4().hex[:12]}"
    got = asyncio.Event()
    conn = await connect(timeout=8)
    try:
        await conn.add_listener(channel, lambda *_: got.set())
        await conn.execute("select pg_notify($1, 'ping')", channel)
        try:
            await asyncio.wait_for(got.wait(), 4)
            return True
        except TimeoutError:
            return False
    finally:
        await conn.close()


async def listen_roundtrip() -> bool:
    """Separada para poder simularla en pruebas."""
    return await _listen_roundtrip()


@check("infra", "infra.pooler_listen")
async def pooler_listen(ctx: Context) -> list[Result]:
    label = "Eventos en vivo entre réplicas (LISTEN/NOTIFY)"
    from app.preflight import checks_infra

    try:
        ok = await checks_infra.listen_roundtrip()
    except Exception as e:  # noqa: BLE001
        return [Result("infra", "infra.pooler_listen", label, "fail",
                       f"No se pudo abrir la conexión dedicada ({type(e).__name__}).")]
    if ok:
        return [Result("infra", "infra.pooler_listen", label, "pass", "La notificación de prueba llegó.")]
    return [Result("infra", "infra.pooler_listen", label, "fail",
                   "La notificación no llegó: DATABASE_URL apunta a un pooler en modo transacción (puerto 6543). "
                   "Usa el «Session pooler» de Supabase (puerto 5432) para que el tiempo real funcione con varias "
                   "réplicas.")]


@check("infra", "infra.pg_cron")
async def pg_cron(ctx: Context) -> list[Result]:
    label = "Tareas programadas (pg_cron)"
    async with ctx.db() as s:
        has = await s.scalar(text("select exists (select 1 from pg_extension where extname = 'pg_cron')"))
        if not has:
            return [Result("infra", "infra.pg_cron", label, "warn",
                           "pg_cron no está instalado: los reportes se recalculan desde el backend (más lento). En "
                           "Supabase actívalo en Database → Extensions → pg_cron y vuelve a aplicar la migración 09.")]
        jobs = set((await s.execute(text("select jobname from cron.job"))).scalars())
    missing = [j for j in EXPECTED_CRON if j not in jobs]
    if missing:
        return [Result("infra", "infra.pg_cron", label, "warn",
                       f"Faltan tareas programadas: {', '.join(missing)}. Vuelve a aplicar las migraciones.",
                       {"jobs": sorted(jobs)})]
    return [Result("infra", "infra.pg_cron", label, "pass", f"{len(jobs)} tareas programadas activas.",
                   {"jobs": sorted(jobs)})]


# --- URLs, secretos y reloj ---------------------------------------------------------------------------------
async def resolve(host: str) -> bool:
    try:
        await asyncio.get_running_loop().getaddrinfo(host, 443)
        return True
    except socket.gaierror:
        return False


@check("infra", "infra.public_urls")
async def public_urls(ctx: Context) -> list[Result]:
    from app.preflight import checks_infra

    out = []
    for key, env_name, value in (("infra.public_base_url", "PUBLIC_BASE_URL", ctx.settings.public_base_url),
                                 ("infra.frontend_base_url", "FRONTEND_BASE_URL", ctx.settings.frontend_base_url)):
        label = f"URL pública ({env_name})"
        u = urlparse(value or "")
        host = u.hostname or ""
        if host in ("localhost", "127.0.0.1") or not host:
            out.append(Result("infra", key, label, "warn",
                              f"{env_name}={value!r} es local: Meta, Stripe y los proveedores no pueden llamar a tus "
                              "webhooks. En producción usa tu dominio con https (ej. https://panel.tuempresa.com).",
                              {"url": value}))
            continue
        if u.scheme != "https":
            out.append(Result("infra", key, label, "fail",
                              f"{env_name} debe usar https (Meta exige https para el webhook).", {"url": value}))
            continue
        ok = await checks_infra.resolve(host)
        out.append(Result("infra", key, label, "pass" if ok else "fail",
                          "El dominio resuelve." if ok else
                          f"El dominio {host} no resuelve: crea el registro DNS A hacia la IP elástica de EC2.",
                          {"url": value}))
    return out


@check("infra", "infra.jwt_secret")
async def jwt_secret(ctx: Context) -> list[Result]:
    label = "Secreto de sesiones (JWT_SECRET)"
    secret = ctx.settings.jwt_secret or ""
    if secret in ("", "change-me") or len(secret) < 32:
        return [Result("infra", "infra.jwt_secret", label, "fail",
                       "JWT_SECRET es el valor por defecto o es corto: genera uno con `openssl rand -hex 32` y "
                       "reinicia (cerrará las sesiones abiertas).", {"length": len(secret)})]
    return [Result("infra", "infra.jwt_secret", label, "pass", "Secreto robusto.", {"length": len(secret)})]


@check("infra", "infra.clock")
async def clock(ctx: Context) -> list[Result]:
    label = "Reloj del servidor"
    try:
        async with ctx.http(5) as http:
            r = await http.head("https://www.google.com")
        remote = parsedate_to_datetime(r.headers["date"])
    except Exception as e:  # noqa: BLE001
        return [Result("infra", "infra.clock", label, "skipped",
                       f"No se pudo comparar con un reloj externo ({type(e).__name__}).")]
    skew = abs((utcnow() - remote).total_seconds())
    status = "pass" if skew < 30 else "warn" if skew < 120 else "fail"
    return [Result("infra", "infra.clock", label, status,
                   f"Desfase de {skew:.0f} s." + ("" if status == "pass" else
                                                    " Activa la sincronización de hora (chrony / Amazon Time Sync): "
                                                    "firmas de Meta, Stripe, SAML y 2FA dependen del reloj."),
                   {"skew_s": round(skew, 1)})]


@check("infra", "infra.resources")
async def resources(ctx: Context) -> list[Result]:
    label = "Disco y memoria"
    du = shutil.disk_usage("/")
    free_pct = round(100 * du.free / du.total, 1)
    data: dict = {"disk_free_pct": free_pct, "disk_free_gb": round(du.free / 1e9, 1)}
    try:
        pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        data["memory_gb"] = round(pages * page_size / 1e9, 1)
    except (ValueError, OSError, AttributeError):
        pass
    status = "pass" if free_pct >= 15 else "warn" if free_pct >= 5 else "fail"
    detail = f"{free_pct} % de disco libre"
    if data.get("memory_gb"):
        detail += f", {data['memory_gb']} GB de memoria"
    if status != "pass":
        detail += ". Libera espacio (`docker system prune`) o amplía el volumen EBS."
    return [Result("infra", "infra.resources", label, status, detail + ".", data)]


@check("infra", "infra.app_version")
async def app_version(ctx: Context) -> list[Result]:
    label = "Versión desplegada (APP_VERSION)"
    v = ctx.settings.app_version
    if not v or v == "dev":
        return [Result("infra", "infra.app_version", label, "warn",
                       "APP_VERSION no está definida: ponle el commit o la etiqueta del despliegue para rastrear "
                       "errores y comparar diagnósticos.", {"version": v})]
    return [Result("infra", "infra.app_version", label, "pass", f"Versión {v}.", {"version": v})]


# --- Supabase ---------------------------------------------------------------------------------------------
def _supabase_headers(ctx: Context, mime: str | None = None) -> dict:
    key = ctx.settings.supabase_secret_key
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if mime:
        h["Content-Type"] = mime
    return h


@check("supabase", "supabase.storage")
async def storage(ctx: Context) -> list[Result]:
    label = "Supabase Storage"
    s = ctx.settings
    if not (s.supabase_url and s.supabase_secret_key):
        return skipped("supabase", "supabase.storage", label,
                       "SUPABASE_URL / SUPABASE_SECRET_KEY no están definidos: los archivos se guardan en disco local "
                       "(solo para desarrollo).")
    base = s.supabase_url.rstrip("/")
    out = []
    async with ctx.http() as http:
        for bucket in BUCKETS:
            key = f"supabase.bucket:{bucket}"
            r = await http.get(f"{base}/storage/v1/bucket/{bucket}", headers=_supabase_headers(ctx))
            if r.status_code != 200:
                out.append(Result("supabase", key, f"Bucket «{bucket}»", "fail",
                                  f"El bucket no existe o la llave no tiene acceso ({error_text(r)}). Aplica la "
                                  "migración 09 o créalo como privado en Storage.", {"bucket": bucket}))
                continue
            public = bool(r.json().get("public"))
            out.append(Result("supabase", key, f"Bucket «{bucket}»", "warn" if public else "pass",
                              "El bucket es PÚBLICO: debe ser privado (los archivos de clientes se sirven con URLs "
                              "firmadas)." if public else "Existe y es privado.", {"bucket": bucket}))
        # Ida y vuelta: sube 1 byte, firma la URL, la descarga y la borra
        path = f"{BUCKETS[0]}/_preflight/{uuid.uuid4().hex}.txt"
        rt = "supabase.storage_roundtrip"
        try:
            up = await http.post(f"{base}/storage/v1/object/{path}", content=b"1",
                                 headers={**_supabase_headers(ctx, "text/plain"), "x-upsert": "true"})
            if up.status_code >= 300:
                out.append(Result("supabase", rt, "Subir y leer un archivo", "fail",
                                  f"No se pudo subir el archivo de prueba ({error_text(up)}): la llave debe ser la "
                                  "«secret key» (sb_secret_…) del proyecto."))
                return out
            sign = await http.post(f"{base}/storage/v1/object/sign/{path}", json={"expiresIn": 60},
                                   headers=_supabase_headers(ctx, "application/json"))
            signed = (sign.json() or {}).get("signedURL") if sign.status_code < 300 else None
            ok = False
            if signed:
                got = await http.get(f"{base}/storage/v1{signed}" if signed.startswith("/") else signed)
                ok = got.status_code == 200 and got.content == b"1"
            out.append(Result("supabase", rt, "Subir y leer un archivo", "pass" if ok else "fail",
                              "Subida, URL firmada y descarga funcionan." if ok else
                              "La URL firmada no devolvió el archivo: revisa las políticas del bucket."))
        finally:
            bucket, _, key = path.partition("/")
            await http.request("DELETE", f"{base}/storage/v1/object/{bucket}", json={"prefixes": [key]},
                               headers=_supabase_headers(ctx, "application/json"))
    return out


@check("supabase", "supabase.vault")
async def vault(ctx: Context) -> list[Result]:
    label = "Supabase Vault (secretos)"
    from app.secrets_vault import delete_secret, get_secret, put_secret

    async with ctx.db() as s:
        has = await s.scalar(text("select to_regnamespace('vault') is not null"))
        if not has:
            return [Result("supabase", "supabase.vault", label, "fail",
                           "La base no tiene el esquema vault: en Supabase viene activo; en otro Postgres instala "
                           "supabase_vault. Sin Vault no se pueden guardar tokens de canales ni llaves de IA.")]
        name = f"preflight:{uuid.uuid4().hex}"
        try:
            sid = await put_secret(s, "ok", name)
            value = await get_secret(s, sid)
            await delete_secret(s, sid)
            await s.commit()
        except Exception as e:  # noqa: BLE001
            await s.rollback()
            return [Result("supabase", "supabase.vault", label, "fail",
                           f"No se pudo escribir/leer un secreto ({type(e).__name__}): el usuario de la base necesita "
                           "permisos sobre vault.create_secret / vault.decrypted_secrets.")]
    ok = value == "ok"
    return [Result("supabase", "supabase.vault", label, "pass" if ok else "fail",
                   "Guardar, leer y borrar un secreto funciona." if ok else
                   "El secreto leído no coincide: revisa la llave de cifrado de Vault.")]


@check("supabase", "supabase.realtime")
async def realtime(ctx: Context) -> list[Result]:
    label = "Supabase Realtime (token del panel)"
    try:
        from app.routers.realtime_token import mint, realtime_enabled
    except ImportError:
        return skipped("supabase", "supabase.realtime", label, "Esta versión no emite tokens de Realtime.")
    if not realtime_enabled():
        return skipped("supabase", "supabase.realtime", label,
                       "REALTIME_TRANSPORT=ws: el panel usa el WebSocket propio. Para Supabase Realtime define "
                       "REALTIME_TRANSPORT=supabase y SUPABASE_JWT_SECRET (Project Settings → API → JWT secret).")
    import jwt

    class _Agent:  # basta con los campos que usa mint()
        id = 0
        organization_id = ctx.organization_id or 0

    token, _exp = mint(_Agent(), str(uuid.uuid4()))
    try:
        claims = jwt.decode(token, ctx.settings.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated")
    except jwt.PyJWTError:
        return [Result("supabase", "supabase.realtime", label, "fail", "El token emitido no se pudo validar.")]
    return [Result("supabase", "supabase.realtime", label, "pass",
                   "El backend emite tokens de Realtime válidos (rol authenticated con la empresa en app_metadata).",
                   {"role": claims.get("role")})]
