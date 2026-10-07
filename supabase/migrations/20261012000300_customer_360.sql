-- =============================================================================
-- 22 · Cliente 360: identidad de WhatsApp (usuario + BSUID), primera/última interacción para BI, flujos y
--      productos por interacción (docs/data-model.md §14)
--
-- Desde 2026 WhatsApp envía en cada webhook el BSUID (user_id, ej. "CO.13491208655302741918", por portafolio) y,
-- si el cliente adoptó un nombre de usuario, profile.username; el teléfono (wa_id) solo llega si hubo interacción
-- con ese número en 30 días o está en la libreta de contactos. Guardamos ambos.
-- Las métricas de la ficha (primera/última interacción, conteos, último agente/tipificación/flujo) se mantienen
-- con triggers (O(1) por mensaje) para que la lista de clientes ordene y filtre sin escanear mensajes.
-- =============================================================================

-- Identidad de WhatsApp ----------------------------------------------------------------------------------
alter table public.contacts
  add column wa_bsuid         text,          -- user_id de WhatsApp (por portafolio)
  add column wa_parent_bsuid  text,          -- parent_user_id (portafolios enlazados), si aplica
  add column wa_username      text;          -- @usuario de WhatsApp, si el cliente lo adoptó
create unique index contacts_bsuid_idx on public.contacts (organization_id, wa_bsuid) where wa_bsuid is not null;
create index contacts_username_trgm on public.contacts using gin (wa_username extensions.gin_trgm_ops) where wa_username is not null;

alter table public.contact_identities
  add column phone        text,              -- teléfono E.164 (solo dígitos) cuando Meta lo envía
  add column bsuid        text,
  add column parent_bsuid text;
create unique index contact_identities_bsuid_idx on public.contact_identities (organization_id, bsuid)
  where provider = 'whatsapp_cloud' and bsuid is not null;
update public.contact_identities set phone = external_id where provider = 'whatsapp_cloud' and phone is null;

-- Cambios de BSUID (user_id_update) y de usuario quedan en el historial de la ficha
alter table public.contact_changes drop constraint contact_changes_source_check;
alter table public.contact_changes add constraint contact_changes_source_check
  check (source in ('agent', 'ai', 'import', 'api', 'flow', 'crm', 'whatsapp', 'system'));

-- Métricas de interacción en la ficha ----------------------------------------------------------------------
alter table public.contacts
  add column first_interaction_at  timestamptz,   -- primer mensaje (cualquier sentido, sin notas de sistema)
  add column first_inbound_at      timestamptz,   -- primer mensaje del cliente
  add column last_interaction_at   timestamptz,
  add column last_inbound_at       timestamptz,
  add column last_outbound_at      timestamptz,
  add column messages_in           int not null default 0,
  add column messages_out          int not null default 0,
  add column conversations_count   int not null default 0,
  add column first_channel_id      bigint references public.channels(id) on delete set null,
  add column last_channel_id       bigint references public.channels(id) on delete set null,
  add column last_conversation_id  bigint references public.conversations(id) on delete set null,
  add column last_agent_id         bigint references public.agents(id) on delete set null,
  add column last_typification_id  bigint references public.typifications(id) on delete set null,
  add column last_typified_at      timestamptz,
  add column flow_runs_count       int not null default 0,
  add column last_flow_id          bigint references public.flows(id) on delete set null,
  add column last_flow_at          timestamptz,
  add column channel_providers     text[] not null default '{}',  -- tipos de canal del cliente (whatsapp, instagram…)
  add column channel_ids           bigint[] not null default '{}', -- números/canales de la empresa con los que habló
                                                                   -- (columna "Canales": "+57 315…" o "2 Canales")
  add column products_count        int not null default 0,
  add column last_product_name     text;
create index contacts_last_interaction_idx on public.contacts (organization_id, last_interaction_at desc nulls last);
create index contacts_first_interaction_idx on public.contacts (organization_id, first_interaction_at);
create index contacts_last_agent_idx on public.contacts (organization_id, last_agent_id) where last_agent_id is not null;
create index contacts_last_typ_idx on public.contacts (organization_id, last_typification_id)
  where last_typification_id is not null;
create index contacts_channels_idx on public.contacts using gin (channel_providers);
create index contacts_channel_ids_idx on public.contacts using gin (channel_ids);
create index contacts_updated_idx on public.contacts (organization_id, updated_at desc);

-- Mensajes → ficha del cliente
create or replace function private.trg_messages_contact_stats() returns trigger
language plpgsql set search_path = public, pg_temp as $$
declare
  v_contact bigint;
  v_channel bigint;
begin
  if new.sender_type = 'system' then
    return null;
  end if;
  select c.contact_id, c.channel_id into v_contact, v_channel from public.conversations c where c.id = new.conversation_id;
  update public.contacts k set
    first_interaction_at = least(coalesce(k.first_interaction_at, new.created_at), new.created_at),
    first_inbound_at = case when new.direction = 'in'
                            then least(coalesce(k.first_inbound_at, new.created_at), new.created_at)
                            else k.first_inbound_at end,
    last_interaction_at = greatest(coalesce(k.last_interaction_at, new.created_at), new.created_at),
    last_inbound_at = case when new.direction = 'in'
                           then greatest(coalesce(k.last_inbound_at, new.created_at), new.created_at)
                           else k.last_inbound_at end,
    last_outbound_at = case when new.direction = 'out'
                            then greatest(coalesce(k.last_outbound_at, new.created_at), new.created_at)
                            else k.last_outbound_at end,
    messages_in = k.messages_in + (new.direction = 'in')::int,
    messages_out = k.messages_out + (new.direction = 'out')::int,
    first_channel_id = coalesce(k.first_channel_id, v_channel),
    last_channel_id = v_channel,
    last_conversation_id = new.conversation_id
  where k.id = v_contact;
  return null;
end $$;
revoke all on function private.trg_messages_contact_stats() from public;
create trigger trg_messages_contact_stats after insert on public.messages
  for each row execute function private.trg_messages_contact_stats();

-- Conversaciones → conteo, último agente y última tipificación
create or replace function private.trg_conversations_contact_stats() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    update public.contacts set conversations_count = conversations_count + 1,
                               last_agent_id = coalesce(new.assigned_agent_id, last_agent_id),
                               channel_ids = case when new.channel_id = any(channel_ids) then channel_ids
                                                  else channel_ids || new.channel_id end
    where id = new.contact_id;
    return null;
  end if;
  if new.assigned_agent_id is distinct from old.assigned_agent_id and new.assigned_agent_id is not null then
    update public.contacts set last_agent_id = new.assigned_agent_id where id = new.contact_id;
  end if;
  if new.typification_id is distinct from old.typification_id and new.typification_id is not null then
    update public.contacts set last_typification_id = new.typification_id, last_typified_at = now()
    where id = new.contact_id;
  end if;
  return null;
end $$;
revoke all on function private.trg_conversations_contact_stats() from public;
create trigger trg_conversations_contact_stats after insert or update of assigned_agent_id, typification_id
  on public.conversations for each row execute function private.trg_conversations_contact_stats();

-- Flujos → ficha
create or replace function private.trg_flow_runs_contact_stats() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if new.contact_id is not null then
    update public.contacts set flow_runs_count = flow_runs_count + 1, last_flow_id = new.flow_id,
                               last_flow_at = new.started_at
    where id = new.contact_id;
  end if;
  return null;
end $$;
revoke all on function private.trg_flow_runs_contact_stats() from public;
create trigger trg_flow_runs_contact_stats after insert on public.flow_runs
  for each row execute function private.trg_flow_runs_contact_stats();

-- Identidades → canales del cliente
create or replace function private.trg_identities_channels() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  update public.contacts k set channel_providers = (
    select coalesce(array_agg(distinct i.provider order by i.provider), '{}')
    from public.contact_identities i where i.contact_id = k.id)
  where k.id = coalesce(new.contact_id, old.contact_id);
  return null;
end $$;
revoke all on function private.trg_identities_channels() from public;
create trigger trg_identities_channels after insert or delete or update of contact_id on public.contact_identities
  for each row execute function private.trg_identities_channels();

-- Productos por interacción -----------------------------------------------------------------------------
-- Un producto mencionado, cotizado o comprado en una conversación: del catálogo (product_id) o de cualquier
-- sistema de la empresa (external_ref = SKU/código del ERP/CRM). Lo registra la IA, el asesor, un flujo, la API
-- o un pedido de WhatsApp.
create table public.interaction_products (
  id               bigint generated by default as identity primary key,
  organization_id  bigint not null references public.organizations(id) on delete cascade,
  contact_id       bigint not null references public.contacts(id) on delete cascade,
  conversation_id  bigint references public.conversations(id) on delete set null,
  message_id       bigint,                      -- mensaje donde apareció (messages es particionada: sin FK)
  product_id       bigint references public.products(id) on delete set null,
  external_ref     text,                        -- código del producto en el sistema de la empresa
  name             text not null,               -- copia del nombre al momento de la interacción
  category         text,
  stage            text not null default 'interested' check (stage in
                     ('mentioned', 'interested', 'quoted', 'purchased', 'not_interested')),
  quantity         numeric(12, 2),
  unit_price       numeric(14, 2),
  currency         char(3),
  source           text not null check (source in ('ai', 'agent', 'flow', 'api', 'whatsapp_order', 'catalog_message',
                                                   'import')),
  confidence       real,                        -- detección por IA
  created_by       bigint references public.agents(id) on delete set null,
  created_at       timestamptz not null default now(),
  check (product_id is not null or external_ref is not null or name <> '')
);
-- Un mismo producto por conversación y etapa (la IA puede volver a detectarlo sin duplicar)
create unique index interaction_products_dedupe_idx on public.interaction_products
  (conversation_id, coalesce(product_id::text, external_ref, lower(name)), stage) where conversation_id is not null;
create index interaction_products_contact_idx on public.interaction_products (organization_id, contact_id, created_at desc);
create index interaction_products_product_idx on public.interaction_products (organization_id, product_id, created_at)
  where product_id is not null;
create index interaction_products_ref_idx on public.interaction_products (organization_id, external_ref)
  where external_ref is not null;

create or replace function private.trg_interaction_products_contact() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  update public.contacts set products_count = products_count + 1, last_product_name = new.name
  where id = new.contact_id;
  return null;
end $$;
revoke all on function private.trg_interaction_products_contact() from public;
create trigger trg_interaction_products_contact after insert on public.interaction_products
  for each row execute function private.trg_interaction_products_contact();

-- Backfill de métricas con los datos existentes ------------------------------------------------------------
update public.contacts k set
  first_interaction_at = s.first_at, first_inbound_at = s.first_in, last_interaction_at = s.last_at,
  last_inbound_at = s.last_in, last_outbound_at = s.last_out, messages_in = s.n_in, messages_out = s.n_out
from (
  select c.contact_id, min(m.created_at) as first_at, min(m.created_at) filter (where m.direction = 'in') as first_in,
         max(m.created_at) as last_at, max(m.created_at) filter (where m.direction = 'in') as last_in,
         max(m.created_at) filter (where m.direction = 'out') as last_out,
         count(*) filter (where m.direction = 'in') as n_in, count(*) filter (where m.direction = 'out') as n_out
  from public.messages m join public.conversations c on c.id = m.conversation_id
  where m.sender_type <> 'system'
  group by c.contact_id
) s
where k.id = s.contact_id;

update public.contacts k set
  conversations_count = s.n, first_channel_id = s.first_channel, last_channel_id = s.last_channel,
  last_conversation_id = s.last_conv, last_agent_id = s.last_agent, last_typification_id = s.last_typ
from (
  select contact_id, count(*) as n,
         (array_agg(channel_id order by created_at))[1] as first_channel,
         (array_agg(channel_id order by last_message_at desc nulls last))[1] as last_channel,
         (array_agg(id order by last_message_at desc nulls last))[1] as last_conv,
         (array_agg(assigned_agent_id order by last_message_at desc nulls last)
            filter (where assigned_agent_id is not null))[1] as last_agent,
         (array_agg(typification_id order by closed_at desc nulls last)
            filter (where typification_id is not null))[1] as last_typ
  from public.conversations group by contact_id
) s
where k.id = s.contact_id;

update public.contacts k set flow_runs_count = s.n, last_flow_id = s.last_flow, last_flow_at = s.last_at
from (select contact_id, count(*) as n, (array_agg(flow_id order by started_at desc))[1] as last_flow,
             max(started_at) as last_at
      from public.flow_runs where contact_id is not null group by contact_id) s
where k.id = s.contact_id;

update public.contacts k set channel_ids = s.ids
from (select contact_id, array_agg(distinct channel_id order by channel_id) as ids
      from public.conversations group by contact_id) s
where k.id = s.contact_id;

update public.contacts k set channel_providers = s.p
from (select contact_id, array_agg(distinct provider order by provider) as p
      from public.contact_identities group by contact_id) s
where k.id = s.contact_id;

-- BI ---------------------------------------------------------------------------------------------------
-- Ficha con tiempos derivados (días entre primera y última interacción, recencia, frecuencia)
create or replace view reporting.v_contact_bi as
select k.id as contact_id, k.organization_id, k.name, k.wa_id as phone, k.wa_username, k.wa_bsuid, k.stage,
       k.created_at, k.updated_at, k.first_interaction_at, k.first_inbound_at, k.last_interaction_at,
       k.last_inbound_at, k.last_outbound_at, k.messages_in, k.messages_out, k.conversations_count,
       k.flow_runs_count, k.products_count, k.channel_providers, k.last_agent_id, k.last_typification_id,
       k.last_flow_id, k.first_channel_id, k.last_channel_id,
       round(extract(epoch from k.last_interaction_at - k.first_interaction_at) / 86400.0, 2) as lifetime_days,
       round(extract(epoch from now() - k.last_interaction_at) / 86400.0, 2) as days_since_last_interaction,
       round(extract(epoch from now() - k.last_inbound_at) / 86400.0, 2) as days_since_last_inbound,
       round(extract(epoch from k.first_interaction_at - k.created_at) / 86400.0, 2) as days_to_first_interaction,
       case when k.conversations_count > 1 then round(extract(epoch from k.last_interaction_at - k.first_interaction_at)
              / 86400.0 / (k.conversations_count - 1), 2) end as avg_days_between_conversations
from public.contacts k;

-- Conversación con sus tiempos (para BI por caso)
create or replace view reporting.v_conversation_bi as
select c.id as conversation_id, c.organization_id, c.contact_id, c.channel_id, c.group_id, c.assigned_agent_id,
       c.typification_id, c.status, c.created_at, c.handoff_at, c.first_response_at, c.closed_at, c.last_message_at,
       c.message_count, c.inbound_count,
       extract(epoch from c.first_response_at - c.handoff_at)::int as first_response_s,
       extract(epoch from coalesce(c.closed_at, c.last_message_at) - c.created_at)::int as duration_s,
       extract(epoch from c.closed_at - c.handoff_at)::int as handling_s,
       (select count(*) from public.interaction_products p where p.conversation_id = c.id) as products,
       (select count(*) from public.flow_runs r where r.conversation_id = c.id) as flow_runs
from public.conversations c;

-- Productos por día (demanda por producto y etapa)
create table reporting.daily_products (
  organization_id  bigint not null,
  day              date not null,
  product_key      text not null,           -- product_id o external_ref o nombre normalizado
  product_id       bigint,
  name             text not null,
  mentioned        int not null default 0,
  interested       int not null default 0,
  quoted           int not null default 0,
  purchased        int not null default 0,
  purchased_value  numeric(16, 2) not null default 0,
  contacts         int not null default 0,
  refreshed_at     timestamptz not null default now(),
  primary key (organization_id, day, product_key)
);

create or replace function reporting.refresh_products(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
declare
  tz  text := (select timezone from public.organizations where id = p_org);
  lo  timestamptz := (p_from::timestamp at time zone tz);
  hi  timestamptz := ((p_to + 1)::timestamp at time zone tz);
begin
  delete from reporting.daily_products where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_products (organization_id, day, product_key, product_id, name, mentioned, interested,
      quoted, purchased, purchased_value, contacts)
  select p_org, (p.created_at at time zone tz)::date,
         coalesce(p.product_id::text, 'ref:' || p.external_ref, 'name:' || lower(p.name)), max(p.product_id),
         max(p.name), count(*) filter (where p.stage = 'mentioned'), count(*) filter (where p.stage = 'interested'),
         count(*) filter (where p.stage = 'quoted'), count(*) filter (where p.stage = 'purchased'),
         coalesce(sum(coalesce(p.quantity, 1) * p.unit_price) filter (where p.stage = 'purchased'), 0),
         count(distinct p.contact_id)
  from public.interaction_products p
  where p.organization_id = p_org and p.created_at >= lo and p.created_at < hi
  group by 2, 3;
end $$;
revoke all on function reporting.refresh_products(bigint, date, date) from public;

create or replace function reporting.refresh_range(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
begin
  perform reporting.refresh_core(p_org, p_from, p_to);
  perform reporting.refresh_flows(p_org, p_from, p_to);
  perform reporting.refresh_links(p_org, p_from, p_to);
  perform reporting.refresh_channels(p_org, p_from, p_to);
  perform reporting.refresh_qa(p_org, p_from, p_to);
  perform reporting.refresh_api(p_org, p_from, p_to);
  perform reporting.refresh_products(p_org, p_from, p_to);
end $$;

-- RLS
alter table public.interaction_products enable row level security;
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'auth') then
    execute 'create policy org_read on public.interaction_products for select to authenticated
             using (organization_id = private.current_org_id())';
  end if;
end $$;
