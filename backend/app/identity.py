"""Identidad de WhatsApp: teléfono + BSUID (user_id por portafolio) + @usuario. docs/data-model.md §14.

Desde 2026 cada webhook trae `user_id` (BSUID) y, si el cliente adoptó un nombre de usuario, `profile.username`;
el teléfono (`wa_id`/`from`) solo llega si hubo interacción con ese número en 30 días o está en la libreta.
Resolución: BSUID → teléfono. Si llegan ambos y apuntan a dos contactos distintos (p. ej. uno importado por
teléfono y otro creado por BSUID), se fusionan: sobrevive el más antiguo y se mueven todas sus referencias.

Envío: `wa_address(contact)` devuelve el teléfono o, si no hay, el BSUID; el cliente de WhatsApp manda el
BSUID en `recipient` (y el teléfono en `to`).
"""

import logging
import re

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ContactChange, ContactIdentity

log = logging.getLogger(__name__)

WHATSAPP = "whatsapp_cloud"
# "CO.13491208655302741918" o padre "CO.ENT.11815799212886844830"
BSUID_RE = re.compile(r"^[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}$")
PHONE_RE = re.compile(r"^[0-9]{8,15}$")
# Plantillas de autenticación con botón de un toque / cero toques / copiar código exigen el teléfono
AUTH_CATEGORY = "AUTHENTICATION"


class IdentityError(Exception):
    pass


def is_bsuid(value: str | None) -> bool:
    return bool(value) and bool(BSUID_RE.match(value))


def clean_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits if PHONE_RE.match(digits) else None


def wa_address(contact: Contact | None) -> str | None:
    """A quién enviar por WhatsApp: el teléfono si lo tenemos (Meta le da prioridad), si no el BSUID."""
    if contact is None:
        return None
    return contact.wa_id or contact.wa_bsuid


def require_phone_for_template(contact: Contact, category: str | None) -> None:
    if (category or "").upper() == AUTH_CATEGORY and not contact.wa_id:
        raise IdentityError("Las plantillas de autenticación necesitan el teléfono del cliente; este contacto solo "
                            "tiene usuario de WhatsApp (BSUID)")


def _change(session: AsyncSession, contact: Contact, key: str, old, new) -> None:
    session.add(ContactChange(organization_id=contact.organization_id, contact_id=contact.id, field_key=key,
                              old_value=None if old is None else str(old), new_value=None if new is None else str(new),
                              source="whatsapp"))


async def _wa_identity(session: AsyncSession, contact: Contact) -> ContactIdentity | None:
    return (await session.scalars(select(ContactIdentity).where(
        ContactIdentity.contact_id == contact.id, ContactIdentity.provider == WHATSAPP)
        .order_by(ContactIdentity.id).limit(1))).first()


async def _sync_identity(session: AsyncSession, contact: Contact) -> None:
    """Una identidad de WhatsApp por contacto con teléfono, BSUID y usuario actuales."""
    ident = await _wa_identity(session, contact)
    external = contact.wa_bsuid or contact.wa_id
    if not external:
        return
    if ident is None:
        session.add(ContactIdentity(organization_id=contact.organization_id, contact_id=contact.id, provider=WHATSAPP,
                                    external_id=external, phone=contact.wa_id, bsuid=contact.wa_bsuid,
                                    parent_bsuid=contact.wa_parent_bsuid, username=contact.wa_username))
        return
    ident.phone, ident.bsuid = contact.wa_id, contact.wa_bsuid
    ident.parent_bsuid, ident.username = contact.wa_parent_bsuid, contact.wa_username
    if ident.external_id != external:
        ident.external_id = external


async def resolve_whatsapp_contact(session: AsyncSession, org: int, phone: str | None = None, bsuid: str | None = None,
                                   parent_bsuid: str | None = None, username: str | None = None,
                                   name: str | None = None) -> tuple[Contact, bool]:
    """Busca o crea el contacto de WhatsApp. Devuelve (contacto, creado)."""
    phone = clean_phone(phone)
    bsuid = bsuid if is_bsuid(bsuid) else None
    parent_bsuid = parent_bsuid if is_bsuid(parent_bsuid) else None
    username = (username or "").strip().lstrip("@") or None
    if not phone and not bsuid:
        raise IdentityError("El mensaje no trae teléfono ni usuario de WhatsApp")

    by_bsuid = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_bsuid == bsuid)) \
        if bsuid else None
    if by_bsuid is None and bsuid:
        ident = await session.scalar(select(ContactIdentity).where(
            ContactIdentity.organization_id == org, ContactIdentity.provider == WHATSAPP,
            ContactIdentity.bsuid == bsuid))
        by_bsuid = await session.get(Contact, ident.contact_id) if ident else None
    by_phone = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_id == phone)) \
        if phone else None

    created = False
    if by_bsuid and by_phone and by_bsuid.id != by_phone.id:
        keep, drop = sorted((by_bsuid, by_phone), key=lambda c: c.id)
        contact = await merge_contacts(session, keep, drop)
    else:
        contact = by_bsuid or by_phone
    if contact is None:
        contact = Contact(organization_id=org, wa_id=phone, wa_bsuid=bsuid, wa_parent_bsuid=parent_bsuid,
                          wa_username=username, name=name or (f"@{username}" if username else None))
        session.add(contact)
        await session.flush()
        await session.refresh(contact, ["field_values", "tag_links"])
        created = True
    else:
        if phone and not contact.wa_id:
            _change(session, contact, "wa_id", None, phone)
            contact.wa_id = phone
        if bsuid and contact.wa_bsuid != bsuid:
            if contact.wa_bsuid:
                _change(session, contact, "wa_bsuid", contact.wa_bsuid, bsuid)
            contact.wa_bsuid = bsuid
        if parent_bsuid and contact.wa_parent_bsuid != parent_bsuid:
            contact.wa_parent_bsuid = parent_bsuid
        if username and contact.wa_username != username:
            _change(session, contact, "wa_username", contact.wa_username, username)
            contact.wa_username = username
        if name and (not contact.name or (contact.wa_username and contact.name == f"@{contact.wa_username}")):
            contact.name = name
    await session.flush()
    await _sync_identity(session, contact)
    await session.flush()
    return contact, created


async def apply_user_id_update(session: AsyncSession, org: int, update: dict) -> Contact | None:
    """Webhook user_id_update: el cliente cambió de número y Meta generó un BSUID nuevo."""
    ids = update.get("user_id") or {}
    previous, current = ids.get("previous"), ids.get("current")
    if not is_bsuid(current):
        return None
    contact = None
    if is_bsuid(previous):
        contact = await session.scalar(select(Contact).where(Contact.organization_id == org,
                                                             Contact.wa_bsuid == previous))
    phone = clean_phone(update.get("wa_id"))
    if contact is None and phone:
        contact = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_id == phone))
    if contact is None:
        return None
    clash = await session.scalar(select(Contact).where(Contact.organization_id == org, Contact.wa_bsuid == current,
                                                       Contact.id != contact.id))
    if clash:  # ya escribió con el BSUID nuevo antes de que llegara el aviso
        contact = await merge_contacts(session, *sorted((contact, clash), key=lambda c: c.id))
    _change(session, contact, "wa_bsuid", contact.wa_bsuid, current)
    contact.wa_bsuid = current
    parent = (update.get("parent_user_id") or {}).get("current")
    if is_bsuid(parent):
        contact.wa_parent_bsuid = parent
    await session.flush()
    await _sync_identity(session, contact)
    await session.flush()
    return contact


# --- Fusión de contactos duplicados -------------------------------------------------------------------
# Tablas con una restricción única que incluye contact_id: antes de mover, se borran las filas del duplicado
# que chocarían con las del contacto que sobrevive.
_UNIQUE_WITH = {"contact_tags": "tag_id", "contact_field_values": "field_id"}
_COPY_FIELDS = ("name", "email", "notes", "memory", "avatar_url", "wa_id", "wa_bsuid", "wa_parent_bsuid",
                "wa_username")


async def merge_contacts(session: AsyncSession, keep: Contact, drop: Contact) -> Contact:
    """Mueve todo lo del contacto `drop` a `keep` y borra `drop`. Las métricas se recalculan desde los hechos."""
    if keep.organization_id != drop.organization_id:
        raise IdentityError("No se pueden fusionar contactos de empresas distintas")
    await session.flush()
    fks = (await session.execute(text("""
        select c.conrelid::regclass::text, a.attname
        from pg_constraint c
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
        where c.contype = 'f' and c.confrelid = 'public.contacts'::regclass and array_length(c.conkey, 1) = 1
          and c.conrelid <> 'public.contacts'::regclass and c.conparentid = 0"""))).all()
    params = {"keep": keep.id, "drop": drop.id}
    for table, column in fks:
        name = table.split(".")[-1]
        if name in _UNIQUE_WITH:
            other = _UNIQUE_WITH[name]
            await session.execute(text(
                f"delete from {table} d using {table} k where d.{column} = :drop and k.{column} = :keep "
                f"and d.{other} = k.{other}"), params)
        if name == "contact_identities":  # la identidad de WhatsApp del duplicado se reemplaza por la del que queda
            await session.execute(text(
                "delete from public.contact_identities where contact_id = :drop and provider = 'whatsapp_cloud'"),
                params)
        await _move_rows(session, table, column, params)

    copied = {f: getattr(drop, f) for f in _COPY_FIELDS if getattr(keep, f) is None and getattr(drop, f) is not None}
    session.add(ContactChange(organization_id=keep.organization_id, contact_id=keep.id, field_key="merge",
                              old_value=f"contacto {drop.id}", new_value=f"fusionado en {keep.id}", source="whatsapp"))
    await session.execute(text("delete from public.contacts where id = :drop"), params)
    session.expunge(drop)
    for f, v in copied.items():
        setattr(keep, f, v)
    await session.flush()
    await recompute_contact_metrics(session, keep.id)
    await session.refresh(keep)
    await session.refresh(keep, ["field_values", "tag_links"])
    log.info("Contactos fusionados: %s → %s", params["drop"], keep.id)
    return keep


async def _move_rows(session: AsyncSession, table: str, column: str, params: dict) -> None:
    """UPDATE en bloque; si choca con una restricción única (p. ej. el mismo destinatario en una campaña o la misma
    llave del registro maestro), fila por fila: la que choca se descarta porque el contacto que queda ya la tiene."""
    try:
        async with session.begin_nested():
            await session.execute(text(f"update {table} set {column} = :keep where {column} = :drop"), params)
        return
    except IntegrityError:
        pass
    ctids = (await session.execute(text(f"select ctid::text from {table} where {column} = :drop"), params)).scalars().all()
    for ctid in ctids:
        try:
            async with session.begin_nested():
                await session.execute(text(f"update {table} set {column} = :keep "
                                           f"where ctid::text = :ctid and {column} = :drop"),
                                      {**params, "ctid": ctid})
        except IntegrityError:
            async with session.begin_nested():
                await session.execute(text(f"delete from {table} where ctid::text = :ctid and {column} = :drop"),
                                      {**params, "ctid": ctid})


async def recompute_contact_metrics(session: AsyncSession, contact_id: int) -> None:
    """Recalcula las métricas de la ficha desde mensajes, conversaciones, flujos y productos."""
    await session.execute(text("""
        update public.contacts k set
          first_interaction_at = s.first_at, first_inbound_at = s.first_in, last_interaction_at = s.last_at,
          last_inbound_at = s.last_in, last_outbound_at = s.last_out,
          messages_in = coalesce(s.n_in, 0), messages_out = coalesce(s.n_out, 0)
        from (select min(m.created_at) as first_at, min(m.created_at) filter (where m.direction = 'in') as first_in,
                     max(m.created_at) as last_at, max(m.created_at) filter (where m.direction = 'in') as last_in,
                     max(m.created_at) filter (where m.direction = 'out') as last_out,
                     count(*) filter (where m.direction = 'in') as n_in,
                     count(*) filter (where m.direction = 'out') as n_out
              from public.messages m join public.conversations c on c.id = m.conversation_id
              where c.contact_id = :id and m.sender_type <> 'system') s
        where k.id = :id"""), {"id": contact_id})
    await session.execute(text("""
        update public.contacts k set
          conversations_count = (select count(*) from public.conversations where contact_id = :id),
          first_channel_id = (select channel_id from public.conversations where contact_id = :id
                              order by created_at limit 1),
          last_channel_id = (select channel_id from public.conversations where contact_id = :id
                             order by last_message_at desc nulls last limit 1),
          last_conversation_id = (select id from public.conversations where contact_id = :id
                                  order by last_message_at desc nulls last limit 1),
          last_agent_id = coalesce((select assigned_agent_id from public.conversations where contact_id = :id
                                    and assigned_agent_id is not null order by last_message_at desc nulls last limit 1),
                                   k.last_agent_id),
          flow_runs_count = (select count(*) from public.flow_runs where contact_id = :id),
          products_count = (select count(*) from public.interaction_products where contact_id = :id),
          channel_providers = (select coalesce(array_agg(distinct provider order by provider), '{}')
                               from public.contact_identities where contact_id = :id),
          channel_ids = (select coalesce(array_agg(distinct channel_id order by channel_id), '{}')
                         from public.conversations where contact_id = :id)
        where k.id = :id"""), {"id": contact_id})
