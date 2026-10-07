-- Stubs mínimos de Supabase para probar las migraciones en un Postgres local (no usar en producción).
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;

create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

create or replace function auth.jwt() returns jsonb language sql stable as
  $$ select coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$;

create schema if not exists realtime;
create table if not exists realtime.messages (
  id bigserial primary key, topic text, event text, payload jsonb, private boolean default true
);
alter table realtime.messages enable row level security;
create or replace function realtime.topic() returns text language sql stable as
  $$ select current_setting('realtime.topic', true) $$;
create or replace function realtime.broadcast_changes(
  topic_name text, event_name text, operation text, table_name text, table_schema text, new record, old record
) returns void language plpgsql as $$
begin
  insert into realtime.messages (topic, event, payload)
  values (topic_name, event_name, jsonb_build_object('table', table_name, 'operation', operation));
end $$;

create schema if not exists storage;
create table if not exists storage.buckets (id text primary key, name text, public boolean, file_size_limit bigint);

-- Vault (misma interfaz que Supabase; sin cifrado: solo para pruebas locales)
create schema if not exists vault;
create table if not exists vault.secrets (
  id uuid primary key default gen_random_uuid(), name text unique, description text default '',
  secret text not null, created_at timestamptz default now(), updated_at timestamptz default now()
);
create or replace function vault.create_secret(new_secret text, new_name text default null,
  new_description text default '', new_key_id uuid default null) returns uuid
language sql as $$ insert into vault.secrets (secret, name, description) values (new_secret, new_name, new_description) returning id $$;
create or replace function vault.update_secret(secret_id uuid, new_secret text default null, new_name text default null,
  new_description text default null, new_key_id uuid default null) returns void
language sql as $$ update vault.secrets set secret = coalesce(new_secret, secret), name = coalesce(new_name, name),
  updated_at = now() where id = secret_id $$;
create or replace view vault.decrypted_secrets as select id, name, description, secret, secret as decrypted_secret,
  created_at, updated_at from vault.secrets;
