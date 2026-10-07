-- =============================================================================
-- 10 · Endurecimiento de seguridad (hallazgos de `supabase db advisors`):
--   - search_path fijo en todas las funciones propias (lint 0011)
--   - extensiones fuera de public (lint 0014)
--   - funciones internas fuera del esquema expuesto por la API (lints 0028/0029):
--     los helpers de RLS y los triggers de Realtime pasan al esquema `private`
-- No se toca public.rls_auto_enable(): la crea Supabase en el proyecto.
-- =============================================================================

-- 1. Extensiones al esquema `extensions` (en Supabase ya existe y está en el search_path)
create schema if not exists extensions;
alter extension pg_trgm set schema extensions;
alter extension citext set schema extensions;

-- 2. Esquema privado: no lo expone PostgREST (/rest/v1/rpc)
create schema if not exists private;
revoke all on schema private from public;

alter function public.set_updated_at() set search_path = public, extensions, pg_temp;
alter function public.ensure_monthly_partitions(regclass, int, date) set search_path = public, pg_temp;
alter function public.drop_old_partitions(regclass, int) set search_path = public, pg_temp;
alter function public.current_actor_type() set search_path = public, pg_temp;
alter function public.current_actor_agent_id() set search_path = public, pg_temp;
alter function public.log_conversation_event(public.conversations, text, jsonb) set search_path = public, pg_temp;
alter function public.trg_conversation_events() set search_path = public, pg_temp;
alter function public.trg_conversation_state() set search_path = public, pg_temp;
alter function public.trg_messages_after_insert() set search_path = public, pg_temp;
alter function public.trg_messages_wa_id() set search_path = public, pg_temp;
alter function public.trg_conversation_tags_events() set search_path = public, pg_temp;
alter function reporting.refresh_range(bigint, date, date) set search_path = reporting, public, pg_temp;
alter function reporting.refresh_recent() set search_path = reporting, public, pg_temp;

-- Las funciones de utilidad y triggers no deben llamarse desde la API
do $$
declare f text;
begin
  foreach f in array array[
    'public.set_updated_at()', 'public.ensure_monthly_partitions(regclass, int, date)',
    'public.drop_old_partitions(regclass, int)', 'public.current_actor_type()', 'public.current_actor_agent_id()',
    'public.log_conversation_event(public.conversations, text, jsonb)', 'public.trg_conversation_events()',
    'public.trg_conversation_state()', 'public.trg_messages_after_insert()', 'public.trg_messages_wa_id()',
    'public.trg_conversation_tags_events()'
  ] loop
    execute format('revoke execute on function %s from public', f);
    -- Supabase otorga EXECUTE explícito a anon/authenticated por default privileges
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke execute on function %s from anon, authenticated', f);
    end if;
  end loop;
end $$;

-- 3. Helpers de RLS y triggers de Realtime (solo existen en Supabase): mover a `private`.
--    Las políticas y triggers los referencian por OID, así que siguen funcionando.
do $guard$
begin
  if to_regprocedure('public.current_org_id()') is not null then
    alter function public.current_org_id() set schema private;
    alter function public.current_agent_role() set schema private;
    alter function private.current_org_id() set search_path = public, pg_temp;
    alter function private.current_agent_role() set search_path = public, pg_temp;
    revoke execute on function private.current_org_id(), private.current_agent_role() from public, anon;
    -- authenticated los necesita para evaluar las políticas RLS
    grant usage on schema private to authenticated;
    grant execute on function private.current_org_id(), private.current_agent_role() to authenticated;
  end if;

  if to_regprocedure('public.rt_broadcast_inbox()') is not null then
    alter function public.rt_broadcast_inbox() set schema private;
    alter function public.rt_broadcast_message() set schema private;
    alter function public.rt_broadcast_alert() set schema private;
    alter function private.rt_broadcast_inbox() set search_path = public, pg_temp;
    alter function private.rt_broadcast_message() set search_path = public, pg_temp;
    alter function private.rt_broadcast_alert() set search_path = public, pg_temp;
    revoke execute on function private.rt_broadcast_inbox(), private.rt_broadcast_message(),
      private.rt_broadcast_alert() from public, anon, authenticated;
  end if;
end $guard$;
