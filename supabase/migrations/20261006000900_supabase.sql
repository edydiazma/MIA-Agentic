-- =============================================================================
-- 09 · Supabase: RLS, Realtime (Broadcast desde la base), Storage y pg_cron.
-- Las partes que dependen de esquemas de Supabase (auth, realtime, storage, cron) están protegidas
-- para que las mismas migraciones se puedan probar en un Postgres local.
-- =============================================================================

-- RLS activo en todas las tablas de negocio (sin políticas = nadie salvo service_role/owner).
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t.tablename);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- Políticas de lectura por organización (requiere Supabase Auth)
-- ---------------------------------------------------------------------------
do $guard$
begin
  if not exists (select 1 from pg_namespace where nspname = 'auth') then
    raise notice 'Sin esquema auth: se omiten políticas RLS de Supabase';
    return;
  end if;

  execute $f$
    create or replace function public.current_org_id() returns bigint
    language sql stable security definer set search_path = public as $$
      select organization_id from public.agents where auth_user_id = auth.uid() and is_active
    $$;
    create or replace function public.current_agent_role() returns text
    language sql stable security definer set search_path = public as $$
      select role from public.agents where auth_user_id = auth.uid() and is_active
    $$;
  $f$;

  -- Tablas con organization_id: lectura para agentes de la misma organización
  declare t record;
  begin
    for t in
      select c.table_name from information_schema.columns c
      join pg_tables p on p.tablename = c.table_name and p.schemaname = 'public'
      where c.table_schema = 'public' and c.column_name = 'organization_id'
        and c.table_name not in ('ai_connections', 'outbound_webhooks', 'integrations', 'org_settings',
                                 'config_revisions', 'inbound_events')
    loop
      execute format('create policy org_read on public.%I for select to authenticated
                      using (organization_id = public.current_org_id())', t.table_name);
    end loop;
  end;

  -- Configuración sensible: solo administradores
  execute $p$
    create policy admin_read on public.ai_connections for select to authenticated
      using (organization_id = public.current_org_id() and public.current_agent_role() = 'admin');
    create policy admin_read on public.outbound_webhooks for select to authenticated
      using (organization_id = public.current_org_id() and public.current_agent_role() = 'admin');
    create policy admin_read on public.integrations for select to authenticated
      using (organization_id = public.current_org_id() and public.current_agent_role() = 'admin');
    create policy admin_read on public.org_settings for select to authenticated
      using (organization_id = public.current_org_id() and public.current_agent_role() = 'admin');
    create policy admin_read on public.config_revisions for select to authenticated
      using (organization_id = public.current_org_id() and public.current_agent_role() = 'admin');
    create policy self_read on public.organizations for select to authenticated
      using (id = public.current_org_id());

    -- Tablas hijas sin organization_id: se resuelve por el padre
    create policy org_read on public.agent_groups for select to authenticated
      using (exists (select 1 from public.groups g where g.id = group_id and g.organization_id = public.current_org_id()));
    create policy org_read on public.contact_field_values for select to authenticated
      using (exists (select 1 from public.contacts c where c.id = contact_id and c.organization_id = public.current_org_id()));
    create policy org_read on public.contact_tags for select to authenticated
      using (exists (select 1 from public.contacts c where c.id = contact_id and c.organization_id = public.current_org_id()));
    create policy org_read on public.conversation_tags for select to authenticated
      using (exists (select 1 from public.conversations c where c.id = conversation_id and c.organization_id = public.current_org_id()));
    create policy org_read on public.campaign_recipients for select to authenticated
      using (exists (select 1 from public.campaigns c where c.id = campaign_id and c.organization_id = public.current_org_id()));
    create policy org_read on public.flow_versions for select to authenticated
      using (exists (select 1 from public.flows f where f.id = flow_id and f.organization_id = public.current_org_id()));
    create policy org_read on public.ai_agent_knowledge for select to authenticated
      using (exists (select 1 from public.ai_agents a where a.id = ai_agent_id and a.organization_id = public.current_org_id()));
    create policy admin_read on public.cortex_members for select to authenticated
      using (exists (select 1 from public.cortexes c where c.id = cortex_id and c.organization_id = public.current_org_id()));
    create policy admin_read on public.ai_connection_health for select to authenticated
      using (exists (select 1 from public.ai_connections c where c.id = connection_id and c.organization_id = public.current_org_id()));
  $p$;

  -- Reporting: el esquema no se expone por PostgREST; lectura vía backend (service_role)
  execute 'revoke all on schema reporting from anon, authenticated';
end $guard$;

-- ---------------------------------------------------------------------------
-- Realtime: Broadcast desde la base en canales privados por organización
-- ---------------------------------------------------------------------------
do $guard$
begin
  if not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'realtime' and p.proname = 'broadcast_changes') then
    raise notice 'Sin realtime.broadcast_changes: se omite Realtime';
    return;
  end if;

  execute $f$
    create or replace function public.rt_broadcast_inbox() returns trigger
    language plpgsql security definer set search_path = public as $$
    begin
      perform realtime.broadcast_changes('org:' || new.organization_id || ':inbox',
        tg_op, tg_op, tg_table_name, tg_table_schema, new, old);
      return null;
    end $$;

    create or replace function public.rt_broadcast_message() returns trigger
    language plpgsql security definer set search_path = public as $$
    begin
      perform realtime.broadcast_changes(
        'org:' || new.organization_id || ':conversation:' || new.conversation_id,
        tg_op, tg_op, tg_table_name, tg_table_schema, new, old);
      return null;
    end $$;

    create or replace function public.rt_broadcast_alert() returns trigger
    language plpgsql security definer set search_path = public as $$
    begin
      perform realtime.broadcast_changes('org:' || new.organization_id || ':alerts',
        tg_op, tg_op, tg_table_name, tg_table_schema, new, old);
      return null;
    end $$;
  $f$;

  execute $t$
    create trigger rt_conversations after insert or update on public.conversations
      for each row execute function public.rt_broadcast_inbox();
    create trigger rt_messages after insert or update on public.messages
      for each row execute function public.rt_broadcast_message();
    create trigger rt_alerts after insert on public.alerts
      for each row execute function public.rt_broadcast_alert();

    -- Solo agentes de la organización pueden escuchar sus canales
    create policy org_topics on realtime.messages for select to authenticated
      using ((select realtime.topic()) like 'org:' || public.current_org_id() || ':%');
  $t$;
end $guard$;

-- ---------------------------------------------------------------------------
-- Storage: buckets privados para medios de conversación y recursos
-- ---------------------------------------------------------------------------
do $guard$
begin
  if to_regclass('storage.buckets') is null then
    raise notice 'Sin Storage: se omiten buckets';
    return;
  end if;
  insert into storage.buckets (id, name, public, file_size_limit)
  values ('conversation-media', 'conversation-media', false, 104857600),
         ('resources', 'resources', false, 16777216)
  on conflict (id) do nothing;
end $guard$;

-- ---------------------------------------------------------------------------
-- pg_cron: rollups, particiones futuras y retención
-- ---------------------------------------------------------------------------
do $guard$
begin
  if not exists (select 1 from pg_available_extensions where name = 'pg_cron') then
    raise notice 'Sin pg_cron: agenda las tareas desde el backend';
    return;
  end if;
  create extension if not exists pg_cron;

  perform cron.schedule('reporting-refresh', '*/10 * * * *', 'select reporting.refresh_recent()');
  perform cron.schedule('partitions-ahead', '0 3 1 * *', $c$
    select public.ensure_monthly_partitions(t::regclass, 3) from unnest(array[
      'public.messages', 'public.conversation_events', 'public.ai_calls', 'public.flow_runs',
      'public.flow_run_steps', 'public.webhook_deliveries', 'public.inbound_events']) t
  $c$);
  perform cron.schedule('retention', '30 3 * * *', $c$
    select public.drop_old_partitions('public.ai_calls', 6),
           public.drop_old_partitions('public.flow_run_steps', 6),
           public.drop_old_partitions('public.webhook_deliveries', 6),
           public.drop_old_partitions('public.inbound_events', 1)
  $c$);
end $guard$;
