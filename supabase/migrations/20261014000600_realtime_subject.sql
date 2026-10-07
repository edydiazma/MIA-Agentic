-- =============================================================================
-- 31b · Panel en Supabase Realtime con token emitido por el backend (docs/data-model.md §19.2)
--
-- El backend firma para cada asesor un JWT corto de Realtime con sub = agents.realtime_subject (uuid estable) y
-- app_metadata.org_id. Para que las políticas de realtime.messages lo reconozcan:
--  · private.current_org_id() también acepta realtime_subject (además de auth_user_id de Supabase Auth);
--  · private.current_agent_id() devuelve el asesor del token;
--  · temas: org:{id}:events (todos los asesores de la empresa), org:{id}:agent:{agent_id} (solo ese asesor) y los
--    temas org:{id}:… que ya publican los triggers (bandeja, conversación, alertas).
-- =============================================================================

create unique index agents_realtime_subject_idx on public.agents (realtime_subject) where realtime_subject is not null;

do $guard$
begin
  if not exists (select 1 from pg_namespace where nspname = 'auth') then
    return;
  end if;
  execute $f$
    create or replace function private.current_org_id() returns bigint
    language sql stable security definer set search_path = public, pg_temp as $$
      select coalesce(
        (select organization_id from public.agents
          where auth_user_id = auth.uid() and is_active
            and organization_id = nullif(auth.jwt() -> 'app_metadata' ->> 'org_id', '')::bigint),
        (select organization_id from public.agents where auth_user_id = auth.uid() and is_active
          order by organization_id limit 1),
        (select organization_id from public.agents where realtime_subject = auth.uid() and is_active))
    $$;

    create or replace function private.current_agent_id() returns bigint
    language sql stable security definer set search_path = public, pg_temp as $$
      select coalesce(
        (select id from public.agents where realtime_subject = auth.uid() and is_active),
        (select id from public.agents where auth_user_id = auth.uid() and is_active
           and organization_id = private.current_org_id() limit 1))
    $$;
  $f$;
  execute 'revoke all on function private.current_agent_id() from public, anon';
  execute 'grant execute on function private.current_agent_id() to authenticated';
end $guard$;

do $guard$
begin
  if to_regclass('realtime.messages') is null or not exists (select 1 from pg_namespace where nspname = 'auth') then
    return;
  end if;
  execute 'drop policy if exists org_topics on realtime.messages';
  -- Temas de la empresa; los de un asesor (…:agent:{id}) solo para ese asesor
  execute $p$
    create policy org_topics on realtime.messages for select to authenticated
      using (
        (select realtime.topic()) like 'org:' || (select private.current_org_id()) || ':%'
        and ((select realtime.topic()) not like '%:agent:%'
             or (select realtime.topic()) = 'org:' || (select private.current_org_id()) || ':agent:'
                                            || (select private.current_agent_id())))
  $p$;
end $guard$;
