-- Pruebas de comportamiento de la fase 2 (atribución, CRM, voz, SaaS). Correr en la base "supa".
\set ON_ERROR_STOP on

do $$
declare
  org2 bigint; ch bigint; ct bigint; cv bigint; call_id bigint; venta bigint; act bigint; team bigint;
  r jsonb; n numeric;
begin
  insert into public.organizations (name, slug, status) values ('Cliente SaaS', 'cliente-saas', 'trial') returning id into org2;
  select id into team from public.plans where key = 'team';
  update public.organizations set plan_id = team where id = org2;
  insert into public.channels (organization_id, name, phone_number_id) values (org2, 'WA', 'PN-SAAS') returning id into ch;
  insert into public.contacts (organization_id, wa_id, name) values (org2, '573005550000', 'Lía') returning id into ct;

  -- Consumo: cada conversación nueva suma al contador del mes
  insert into public.conversations (organization_id, contact_id, channel_id) values (org2, ct, ch) returning id into cv;
  select value into n from public.usage_counters where organization_id = org2 and metric = 'conversations';
  assert n = 1, 'contador de conversaciones';
  r := public.check_limit(org2, 'channels');
  assert (r->>'used')::int = 1 and (r->>'limit')::int = 2 and (r->>'allowed')::boolean, 'límite de canales';
  insert into public.channels (organization_id, name, phone_number_id) values (org2, 'WA2', 'PN-SAAS-2');
  assert not (public.check_limit(org2, 'channels')->>'allowed')::boolean, 'límite alcanzado';
  assert (public.check_limit(org2, 'users')->>'allowed')::boolean, 'usuarios ilimitados (null)';

  -- Llamada: duración calculada, eventos y minutos de voz
  insert into public.calls (organization_id, channel_id, contact_id, conversation_id, wa_call_id, direction)
    values (org2, ch, ct, cv, 'wacid.1', 'inbound') returning id into call_id;
  assert exists (select 1 from public.conversation_events where conversation_id = cv and event_type = 'call_started'), 'call_started';
  update public.calls set status = 'connected', handled_by = 'voice_agent', answered_at = now() - interval '125 seconds'
    where id = call_id;
  update public.calls set status = 'ended', ended_at = now() where id = call_id;
  assert (select duration_s from public.calls where id = call_id) between 124 and 126, 'duración';
  assert exists (select 1 from public.conversation_events where conversation_id = cv and event_type = 'call_ended'), 'call_ended';
  select value into n from public.usage_counters where organization_id = org2 and metric = 'voice_minutes';
  assert n = 3, 'minutos de voz redondeados hacia arriba';

  -- Atribución única por conversación y conversiones sin duplicados
  insert into public.attributions (organization_id, conversation_id, contact_id, channel, matched_by, gclid)
    values (org2, cv, ct, 'google_ads', 'ref_code', 'gclid-123');
  begin
    insert into public.attributions (organization_id, conversation_id, contact_id, channel, matched_by)
      values (org2, cv, ct, 'direct', 'none');
    raise exception 'debía fallar: dos atribuciones';
  exception when unique_violation then null;
  end;
  insert into public.typifications (organization_id, name, is_success) values (org2, 'Venta', true) returning id into venta;
  insert into public.conversion_actions (organization_id, name, trigger, typification_id, google_ads)
    values (org2, 'Venta WA', 'typification', venta, '{"customer_id": "123", "conversion_action_id": "456"}') returning id into act;
  insert into public.conversion_events (organization_id, action_id, conversation_id, contact_id, currency, dedupe_key)
    values (org2, act, cv, ct, 'COP', 'conv-' || cv);
  begin
    insert into public.conversion_events (organization_id, action_id, conversation_id, contact_id, currency, dedupe_key)
      values (org2, act, cv, ct, 'COP', 'conv-' || cv);
    raise exception 'debía fallar: conversión duplicada';
  exception when unique_violation then null;
  end;

  -- Negocio ganado exige fecha de cierre
  begin
    insert into public.deals (organization_id, contact_id, name, status) values (org2, ct, 'Onix', 'won');
    raise exception 'debía fallar: ganado sin closed_at';
  exception when check_violation then null;
  end;
  insert into public.deals (organization_id, contact_id, name, status, amount, closed_at)
    values (org2, ct, 'Onix', 'won', 89900000, now());

  -- Un mismo usuario de Auth en dos empresas
  insert into public.agents (organization_id, email, name, role, auth_user_id)
    values (1, 'multi@x.com', 'Multi', 'agent', '00000000-0000-0000-0000-0000000000aa'),
           (org2, 'multi@x.com', 'Multi', 'admin', '00000000-0000-0000-0000-0000000000aa');
  raise notice 'OK: fase 2 (consumo, límites, llamadas, atribución, conversiones, negocios, multiempresa)';
end $$;

-- RLS con empresa activa en el JWT (el id se resuelve antes de cambiar de rol: después ya aplica RLS)
select id as saas_org from public.organizations where slug = 'cliente-saas' \gset
grant usage on schema public to authenticated;
grant select on all tables in schema public to authenticated;
set role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000aa', false);
select set_config('request.jwt.claims', json_build_object('app_metadata', json_build_object('org_id', :saas_org))::text, false);
do $$
begin
  assert (select count(distinct organization_id) from public.contacts) = 1
     and (select min(organization_id) from public.contacts) = (select id from public.organizations), 'RLS: solo la empresa activa';
  assert (select count(*) from public.calls) = 1, 'RLS: llamadas de la empresa activa';
  raise notice 'OK: RLS multiempresa por JWT';
end $$;
reset role;
