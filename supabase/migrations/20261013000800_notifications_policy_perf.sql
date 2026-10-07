-- =============================================================================
-- 29b · RLS de notifications evaluada una vez por consulta (advisor auth_rls_initplan): auth.uid() y la empresa
-- actual envueltos en (select …) en lugar de recalcularse por fila. docs/data-model.md §18.3
-- =============================================================================
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'auth') then
    execute 'drop policy if exists own_read on public.notifications';
    execute 'create policy own_read on public.notifications for select to authenticated
             using (organization_id = (select private.current_org_id()) and agent_id in
                    (select id from public.agents where auth_user_id = (select auth.uid())))';
  end if;
end $$;
