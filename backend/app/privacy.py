"""Solicitudes de titulares de datos (habeas data — Ley 1581 de 2012): exportar y eliminar los datos de un cliente.

- export_contact: JSON con TODO lo que la empresa tiene del cliente (ficha, identidades, campos, etiquetas, registro
  maestro con llaves, vehículos y consentimientos, conversaciones con sus mensajes, atribución, negocios, citas,
  seguimientos, productos, llamadas). Sin secretos ni datos de otros clientes.
- erase_contact: anonimización IRREVERSIBLE. Borra o vacía todo dato personal (textos, medios, identificadores,
  llaves, vehículos, transcripciones, grabaciones) y conserva solo lo necesario para que los agregados y reportes
  sigan cuadrando (conteos de conversaciones/mensajes, canal, tipificación, montos de negocios, productos).
  Deja constancia en contact_changes (campo "erased") sin datos personales.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, utcnow

log = logging.getLogger(__name__)

ERASED_NAME = "[eliminado]"

# Tablas con contact_id que se exportan tal cual (columnas sensibles de sistema excluidas)
EXPORT_TABLES = (
    "contact_identities", "contact_field_values", "contact_tags", "contact_changes", "contact_keys", "contact_golden",
    "contact_vehicles", "contact_consents", "interaction_products", "attributions", "deals", "appointments",
    "followups", "calls", "campaign_recipients", "call_permissions", "webchat_sessions",
)
SKIP_COLUMNS = {"token_hash", "access_token_secret_id", "refresh_token_secret_id", "ip_hash"}


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v


def _row(r) -> dict:
    return {k: _jsonable(v) for k, v in dict(r._mapping).items() if k not in SKIP_COLUMNS}


async def _table_exists(session: AsyncSession, table: str) -> bool:
    return bool(await session.scalar(text(
        "select 1 from information_schema.tables where table_schema = 'public' and table_name = :t"), {"t": table}))


async def export_contact(session: AsyncSession, contact: Contact) -> dict:
    org, cid = contact.organization_id, contact.id
    out: dict = {"generated_at": utcnow().isoformat(), "organization_id": org,
                 "contact": _row((await session.execute(text("select * from public.contacts where id = :c"),
                                                         {"c": cid})).one())}
    for table in EXPORT_TABLES:
        if not await _table_exists(session, table):
            continue
        rows = (await session.execute(text(f"select * from public.{table} where contact_id = :c"), {"c": cid})).all()
        out[table] = [_row(r) for r in rows]
    convs = (await session.execute(text(
        "select * from public.conversations where contact_id = :c and organization_id = :o order by created_at"),
        {"c": cid, "o": org})).all()
    conversations = []
    for c in convs:
        conv = _row(c)
        msgs = (await session.execute(text(
            "select id, created_at, direction, sender_type, type, text, transcript, media_mime, media_filename, "
            "template_name, status, metadata from public.messages where conversation_id = :v order by created_at"),
            {"v": conv["id"]})).all()
        conv["messages"] = [_row(m) for m in msgs]
        conversations.append(conv)
    out["conversations"] = conversations
    return out


async def erase_contact(session: AsyncSession, contact: Contact, agent_id: int | None) -> dict:
    """Anonimiza al cliente. Devuelve conteos de lo eliminado/vaciado. El llamador hace commit."""
    from app import storage

    org, cid = contact.organization_id, contact.id
    stats: dict[str, int] = {}

    async def run(name: str, sql: str, **params) -> None:
        r = await session.execute(text(sql), {"c": cid, "o": org, **params})
        stats[name] = stats.get(name, 0) + (r.rowcount or 0)

    conv_ids = [r[0] for r in (await session.execute(text(
        "select id from public.conversations where contact_id = :c and organization_id = :o"),
        {"c": cid, "o": org})).all()]

    # Archivos en Storage (medios de mensajes, grabaciones de llamadas)
    paths = []
    if conv_ids:
        paths += [r[0] for r in (await session.execute(text(
            "select media_path from public.messages where conversation_id = any(:ids) and media_path is not null"),
            {"ids": conv_ids})).all()]
    if await _table_exists(session, "calls"):
        rec = (await session.execute(text(
            "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'calls'"
            " and column_name = 'recording_path'"))).scalar()
        if rec:
            paths += [r[0] for r in (await session.execute(text(
                "select recording_path from public.calls where contact_id = :c and recording_path is not null"),
                {"c": cid})).all()]
    removed = 0
    for path in paths:
        try:
            await storage.remove(path)
            removed += 1
        except Exception:  # noqa: BLE001 — el borrado de datos no se detiene por un archivo
            log.warning("No se pudo borrar el archivo %s", path)
    stats["files_removed"] = removed

    # Mensajes: se conservan las filas (conteos, reportes) sin contenido
    if conv_ids:
        await run("messages", "update public.messages set text = null, transcript = null, media_path = null, "
                  "media_filename = null, metadata = null where conversation_id = any(:ids)", ids=conv_ids)
        await run("conversations", "update public.conversations set last_message_preview = null, "
                  "ad_headline = null, ad_source_url = null, ad_ctwa_clid = null, summary = null, handoff_summary = null "
                  "where id = any(:ids)", ids=conv_ids)
        await run("copilot_suggestions", "delete from public.copilot_suggestions where conversation_id = any(:ids)",
                  ids=conv_ids)
        await run("conversation_reviews", "update public.conversation_reviews set summary = null, dispute_note = null, scores = '{}'::jsonb "
                  "where conversation_id = any(:ids)", ids=conv_ids)
        await run("email_threads", "delete from public.email_threads where conversation_id = any(:ids)", ids=conv_ids)

    # Datos personales directos
    for table in ("contact_identities", "contact_field_values", "contact_tags", "contact_changes", "contact_keys",
                  "contact_vehicles", "contact_consents", "key_extractions", "webchat_sessions", "call_permissions"):
        if await _table_exists(session, table):
            await run(table, f"delete from public.{table} where contact_id = :c")
    await run("contact_golden", "delete from public.contact_golden where contact_id = :c")
    await run("merge_candidates", "delete from public.contact_merge_candidates where contact_a_id = :c "
              "or contact_b_id = :c")
    await run("attributions", "update public.attributions set gclid = null, gbraid = null, wbraid = null, fbc = null, "
              "fbp = null, ctwa_clid = null, landing_url = null, source_url = null where contact_id = :c")
    await run("attribution_touches", "update public.attribution_touches set gclid = null, ctwa_clid = null "
              "where contact_id = :c")
    await run("deals", "update public.deals set name = :n where contact_id = :c", n=ERASED_NAME)
    await run("appointments", "update public.appointments set notes = null, title = :n where contact_id = :c",
              n=ERASED_NAME)
    await run("followups", "update public.followups set note = :n where contact_id = :c", n=ERASED_NAME)
    if await _table_exists(session, "calls"):
        cols = {r[0] for r in (await session.execute(text(
            "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'calls'"
        ))).all()}
        sets = [f"{c} = null" for c in ("recording_path", "summary", "transcript") if c in cols]
        if sets:
            await run("calls", f"update public.calls set {', '.join(sets)} where contact_id = :c")
        if await _table_exists(session, "call_turns"):
            await run("call_turns", "delete from public.call_turns where call_id in "
                      "(select id from public.calls where contact_id = :c)")

    # Ficha: sin identificadores ni texto libre; se conserva la fila (agregados, conteos, canal, etapa)
    contact.name = ERASED_NAME
    contact.email = None
    contact.notes = None
    contact.memory = None
    contact.wa_id = None
    contact.wa_bsuid = None
    contact.wa_parent_bsuid = None
    contact.wa_username = None
    contact.avatar_url = None
    contact.last_product_name = None
    contact.marketing_opt_out = True
    contact.opt_out_at = contact.opt_out_at or utcnow()
    await session.flush()
    # Constancia sin datos personales (quién y cuándo)
    await session.execute(text(
        "insert into public.contact_changes (organization_id, contact_id, field_key, old_value, new_value, source, agent_id) "
        "values (:o, :c, 'erased', null, 'Datos personales eliminados por solicitud del titular', 'agent', :a)"),
        {"o": org, "c": cid, "a": agent_id})
    return stats
