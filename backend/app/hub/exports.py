"""Exportación de datos al almacén del cliente (BigQuery, S3, GCS) o descarga.

- Solo los conjuntos de DATASETS (lista blanca de columnas). Las columnas sensibles (teléfono, correo, texto de
  mensajes, identificadores de clic) se excluyen salvo `include_sensitive` + permiso «exports.data».
- Formato: parquet si pyarrow está instalado; si no, CSV o JSONL (BigQuery siempre JSONL).
- Incremental: filas con updated_at/created_at > marca de agua de la última corrida exitosa.
- Credenciales en Vault: config.secret_ids {access_key_id, secret_access_key} (S3) o {service_account} (GCS/BigQuery).
"""

import csv
import hashlib
import hmac
import io
import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app import storage
from app.hub import http
from app.hub.http import HubError
from app.models import DataExport, DataExportRun, utcnow
from app.secrets_vault import get_secret, put_secret

DESTINATIONS = ("download", "s3", "gcs", "bigquery", "sftp")
SCHEDULES = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7), "manual": None}
FORMATS = ("parquet", "csv", "jsonl")
BATCH = 5000
MAX_ROWS = 500_000

# dataset: (modelo, columna de marca de agua, columnas, columnas sensibles)
DATASETS: dict[str, tuple] = {
    "contacts": (m.Contact, "updated_at", [
        "id", "stage", "first_interaction_at", "last_interaction_at", "messages_in", "messages_out",
        "conversations_count", "products_count", "first_source_channel", "first_source_campaign", "first_source_at",
        "last_source_channel", "last_source_campaign", "last_source_at", "owner_agent_id", "marketing_opt_out",
        "created_at", "updated_at"], ["wa_id", "name", "email", "notes"]),
    "conversations": (m.Conversation, "updated_at", [
        "id", "contact_id", "channel_id", "status", "assigned_agent_id", "typification_id", "first_response_at",
        "closed_at", "message_count", "inbound_count", "ad_source_type", "ad_source_id", "ai_sentiment", "qa_score",
        "is_returning", "created_at", "updated_at"], ["ai_summary", "summary"]),
    "messages": (m.Message, "created_at", [
        "id", "conversation_id", "direction", "sender_type", "sender_agent_id", "type", "template_name",
        "campaign_id", "status", "pricing_category", "billable", "created_at"], ["text", "transcript"]),
    "deals": (m.Deal, "updated_at", [
        "id", "contact_id", "conversation_id", "owner_agent_id", "name", "amount", "currency", "pipeline", "stage",
        "status", "source", "expected_close", "closed_at", "lost_reason", "created_at", "updated_at"], []),
    "attributions": (m.Attribution, "created_at", [
        "id", "conversation_id", "contact_id", "channel", "matched_by", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "utm_term", "ad_id", "platform_campaign_id", "platform_campaign_name", "ad_group_name",
        "ad_name", "keyword", "source_type", "created_at"], ["gclid", "gbraid", "wbraid", "fbc", "fbp", "ctwa_clid",
                                                            "landing_url"]),
    "appointments": (m.Appointment, "updated_at", [
        "id", "contact_id", "conversation_id", "agent_id", "starts_at", "duration_min", "title", "status",
        "created_by_type", "created_at", "updated_at"], ["notes"]),
    "orders": (m.ExternalOrder, "updated_at", [
        "id", "connection_id", "external_id", "order_number", "contact_id", "status", "total", "currency", "items",
        "attribution_id", "placed_at", "updated_at"], ["customer"]),
    "interaction_products": (m.InteractionProduct, "created_at", [
        "id", "contact_id", "conversation_id", "product_id", "external_ref", "name", "category", "stage", "quantity",
        "unit_price", "currency", "source", "created_at"], []),
    "products": (m.Product, "updated_at", [
        "id", "sku", "name", "category", "brand", "price", "sale_price", "currency", "stock", "available", "source",
        "created_at", "updated_at"], []),
    "campaigns": (m.Campaign, "created_at", [
        "id", "channel_id", "name", "template_name", "status", "scheduled_at", "started_at", "finished_at", "source",
        "created_at"], []),
}
SECRETS = {"s3": ("access_key_id", "secret_access_key"), "gcs": ("service_account",),
           "bigquery": ("service_account",), "sftp": ("password",), "download": ()}


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def effective_format(export: DataExport) -> str:
    if export.destination == "bigquery":
        return "jsonl"
    if export.format == "parquet" and not parquet_available():
        return "csv"
    return export.format if export.format in FORMATS else "csv"


def validate(destination: str, datasets: list[str], fmt: str, schedule: str, config: dict) -> list[str]:
    errors = []
    if destination not in DESTINATIONS:
        errors.append(f"Destino inválido ({', '.join(DESTINATIONS)})")
    bad = [d for d in datasets if d not in DATASETS]
    if bad or not datasets:
        errors.append(f"Conjuntos inválidos: {', '.join(bad) or 'ninguno'}")
    if fmt not in FORMATS:
        errors.append(f"Formato inválido ({', '.join(FORMATS)})")
    if schedule not in SCHEDULES:
        errors.append(f"Frecuencia inválida ({', '.join(SCHEDULES)})")
    need = {"s3": ("bucket", "region"), "gcs": ("bucket",), "bigquery": ("project_id", "dataset"),
            "sftp": ("host", "username")}.get(destination, ())
    errors += [f"config.{k} es obligatorio" for k in need if not config.get(k)]
    if destination == "sftp":
        errors.append("SFTP no está disponible en este servidor (falta la librería paramiko)")
    return errors


async def save_secrets(session: AsyncSession, export: DataExport, values: dict[str, str | None]) -> None:
    cfg = dict(export.config or {})
    ids = dict(cfg.get("secret_ids") or {})
    for name in SECRETS.get(export.destination, ()):
        v = values.get(name)
        if v:
            ids[name] = await put_secret(session, v, f"export:{export.id}:{name}", ids.get(name))
    cfg["secret_ids"] = ids
    export.config = cfg


async def _secret(session: AsyncSession, export: DataExport, name: str) -> str | None:
    sid = ((export.config or {}).get("secret_ids") or {}).get(name)
    return await get_secret(session, sid) if sid else None


def public_config(export: DataExport) -> dict:
    cfg = {k: v for k, v in (export.config or {}).items() if k != "secret_ids"}
    cfg["secrets_set"] = sorted(((export.config or {}).get("secret_ids") or {}).keys())
    return cfg


def due(export: DataExport, now: datetime | None = None) -> bool:
    every = SCHEDULES.get(export.schedule)
    if not export.is_active or every is None:
        return False
    return export.last_export_at is None or (now or utcnow()) - export.last_export_at >= every - timedelta(minutes=5)


# --- Lectura ---------------------------------------------------------------------------------------------------
def columns(dataset: str, include_sensitive: bool) -> list[str]:
    _, _, cols, sensitive = DATASETS[dataset]
    return cols + (sensitive if include_sensitive else [])


def _val(v):
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


async def rows(session: AsyncSession, org: int, dataset: str, since: datetime | None, until: datetime,
               include_sensitive: bool) -> list[dict]:
    model, wm, _, _ = DATASETS[dataset]
    cols = columns(dataset, include_sensitive)
    table = model.__table__
    wcol = table.c[wm]
    out, last_id = [], 0
    while len(out) < MAX_ROWS:
        q = select(*[table.c[c] for c in cols]).where(table.c.organization_id == org, wcol <= until,
                                                       table.c.id > last_id)
        if since:
            q = q.where(wcol > since)
        batch = (await session.execute(q.order_by(table.c.id).limit(BATCH))).mappings().all()
        if not batch:
            break
        out += [{c: _val(r[c]) for c in cols} for r in batch]
        last_id = batch[-1]["id"]
        if len(batch) < BATCH:
            break
    return out


def encode(data: list[dict], cols: list[str], fmt: str) -> tuple[bytes, str, str]:
    """→ (bytes, mime, extensión)."""
    if fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist([{c: (json.dumps(r[c], default=str) if isinstance(r[c], (dict, list)) else r[c])
                                       for c in cols} for r in data])
        buf = io.BytesIO()
        pq.write_table(table, buf)
        return buf.getvalue(), "application/vnd.apache.parquet", "parquet"
    if fmt == "jsonl":
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in data)
        return body.encode(), "application/x-ndjson", "jsonl"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in data:
        w.writerow({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
                    for k, v in r.items()})
    return buf.getvalue().encode(), "text/csv", "csv"


# --- Destinos ----------------------------------------------------------------------------------------------------
def _s3_sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


async def put_s3(cfg: dict, access_key: str, secret_key: str, key: str, data: bytes, mime: str) -> str:
    """PUT con firma SigV4 (sin boto3). config.endpoint opcional para compatibles (R2, MinIO, Spaces)."""
    region, bucket = cfg["region"], cfg["bucket"]
    host = (cfg.get("endpoint") or f"https://{bucket}.s3.{region}.amazonaws.com").removeprefix("https://").rstrip("/")
    path = ("/" + bucket if cfg.get("endpoint") else "") + "/" + quote(key)
    now = datetime.now(UTC)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date, "content-type": mime}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["PUT", path, "", *[f"{k}:{headers[k]}" for k in sorted(headers)], "", signed, payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _s3_sign(_s3_sign(_s3_sign(_s3_sign(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    http.check(await http.send("PUT", f"https://{host}{path}", headers={**{k2: v for k2, v in headers.items()
                                                                            if k2 != "host"}, "Authorization": auth},
                               content=data, timeout=120), "S3")
    return f"s3://{bucket}/{key}"


async def google_token(service_account_json: str, scope: str) -> str:
    try:
        sa = json.loads(service_account_json)
    except ValueError as e:
        raise HubError("La cuenta de servicio no es un JSON válido", retryable=False) from e
    now = int(time.time())
    assertion = jwt.encode({"iss": sa["client_email"], "scope": scope, "aud": sa.get("token_uri") or
                            "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600},
                           sa["private_key"], algorithm="RS256")
    r = http.check(await http.send("POST", sa.get("token_uri") or "https://oauth2.googleapis.com/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}), "Google")
    return r.json()["access_token"]


async def put_gcs(cfg: dict, sa: str, key: str, data: bytes, mime: str) -> str:
    token = await google_token(sa, "https://www.googleapis.com/auth/devstorage.read_write")
    http.check(await http.send(
        "POST", f"https://storage.googleapis.com/upload/storage/v1/b/{quote(cfg['bucket'], safe='')}/o",
        headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
        params={"uploadType": "media", "name": key}, content=data, timeout=120), "GCS")
    return f"gs://{cfg['bucket']}/{key}"


async def load_bigquery(cfg: dict, sa: str, table: str, data: bytes) -> str:
    """Trabajo de carga (JSONL, autodetect, WRITE_APPEND) por subida multipart. Devuelve el id del job."""
    token = await google_token(sa, "https://www.googleapis.com/auth/bigquery")
    meta = {"configuration": {"load": {
        "destinationTable": {"projectId": cfg["project_id"], "datasetId": cfg["dataset"],
                             "tableId": f"{cfg.get('table_prefix', 'wa_')}{table}"},
        "sourceFormat": "NEWLINE_DELIMITED_JSON", "autodetect": True, "writeDisposition": "WRITE_APPEND",
        "createDisposition": "CREATE_IF_NEEDED", "schemaUpdateOptions": ["ALLOW_FIELD_ADDITION"]}}}
    if cfg.get("location"):
        meta["jobReference"] = {"location": cfg["location"]}
    boundary = "wa-export-" + hashlib.sha1(data[:64] + str(time.time()).encode()).hexdigest()[:16]
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + \
        f"\r\n--{boundary}--".encode()
    r = http.check(await http.send(
        "POST", f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{cfg['project_id']}/jobs",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
        params={"uploadType": "multipart"}, content=body, timeout=120), "BigQuery")
    job = r.json()
    if (job.get("status") or {}).get("errorResult"):
        raise HubError(f"BigQuery: {job['status']['errorResult'].get('message')}", retryable=False)
    return (job.get("jobReference") or {}).get("jobId") or ""


# --- Corrida -------------------------------------------------------------------------------------------------
async def last_watermark(session: AsyncSession, export: DataExport) -> datetime | None:
    return await session.scalar(select(DataExportRun.watermark).where(
        DataExportRun.export_id == export.id, DataExportRun.status == "succeeded")
        .order_by(DataExportRun.started_at.desc()).limit(1))


async def run_export(session: AsyncSession, export: DataExport, full: bool = False) -> DataExportRun:
    until = utcnow()
    since = None if (full or not export.incremental) else await last_watermark(session, export)
    run = DataExportRun(export_id=export.id, status="running", watermark=until, files=[])
    session.add(run)
    await session.flush()
    fmt = effective_format(export)
    cfg = export.config or {}
    stamp = until.strftime("%Y%m%dT%H%M%SZ")
    try:
        if export.destination == "sftp":
            raise HubError("SFTP no está disponible en este servidor", retryable=False)
        creds = {n: await _secret(session, export, n) for n in SECRETS.get(export.destination, ())}
        if any(v is None for v in creds.values()):
            raise HubError("Faltan las credenciales del destino", retryable=False)
        for ds in export.datasets or []:
            if ds not in DATASETS:
                continue
            cols = columns(ds, export.include_sensitive)
            data = await rows(session, export.organization_id, ds, since, until, export.include_sensitive)
            if not data:
                run.files = [*run.files, {"dataset": ds, "rows": 0}]
                continue
            blob, mime, ext = encode(data, cols, fmt)
            key = f"{(cfg.get('prefix') or 'wa-export').strip('/')}/{ds}/{ds}_{stamp}.{ext}"
            if export.destination == "s3":
                target = await put_s3(cfg, creds["access_key_id"], creds["secret_access_key"], key, blob, mime)
            elif export.destination == "gcs":
                target = await put_gcs(cfg, creds["service_account"], key, blob, mime)
            elif export.destination == "bigquery":
                target = "bigquery:job/" + await load_bigquery(cfg, creds["service_account"], ds, blob)
            else:
                path = f"exports/{export.organization_id}/{export.id}/{ds}_{stamp}.{ext}"
                await storage.upload(path, blob, mime)
                target = path
            run.files = [*run.files, {"dataset": ds, "rows": len(data), "bytes": len(blob), "target": target,
                                      "format": ext}]
            run.rows_exported += len(data)
            run.bytes += len(blob)
        run.status = "succeeded"
        export.last_status, export.last_error = "succeeded", None
    except HubError as e:
        run.status, run.error = "failed", str(e)[:1000]
        export.last_status, export.last_error = "failed", str(e)[:500]
    except Exception as e:  # noqa: BLE001
        run.status, run.error = "failed", f"{type(e).__name__}: {e}"[:1000]
        export.last_status, export.last_error = "failed", run.error[:500]
    run.finished_at = utcnow()
    export.last_export_at = until
    await session.commit()
    return run


def run_json(r: DataExportRun) -> dict:
    return {"id": r.id, "status": r.status, "rows_exported": r.rows_exported, "bytes": r.bytes,
            "files": r.files or [], "watermark": r.watermark, "error": r.error, "started_at": r.started_at,
            "finished_at": r.finished_at}


def export_json(e: DataExport) -> dict:
    return {"id": e.id, "name": e.name, "destination": e.destination, "config": public_config(e),
            "datasets": e.datasets or [], "format": e.format, "effective_format": effective_format(e),
            "schedule": e.schedule, "incremental": e.incremental, "include_sensitive": e.include_sensitive,
            "last_export_at": e.last_export_at, "last_status": e.last_status, "last_error": e.last_error,
            "is_active": e.is_active, "created_at": e.created_at}


def catalog() -> list[dict]:
    return [{"key": k, "columns": v[2], "sensitive_columns": v[3], "watermark": v[1]} for k, v in DATASETS.items()]
