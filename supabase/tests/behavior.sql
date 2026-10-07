-- Pruebas de comportamiento del modelo (correr en la base "supa" de apply_local.sh).
-- Falla con excepción si algo no cumple.
\set ON_ERROR_STOP on

do $$
declare
  ag bigint; ch bigint; ct bigint; cv bigint; venta bigint; tg bigint; f_num bigint;
  c public.conversations;
  n int;
  t0 timestamptz := now() - interval '30 minutes';
begin
  insert into public.agents (organization_id, email, name, role, auth_user_id)
    values (1, 'luis@x.com', 'Luis', 'agent', '00000000-0000-0000-0000-000000000001') returning id into ag;
  insert into public.channels (organization_id, name, phone_number_id) values (1, 'WA', 'PN1') returning id into ch;
  insert into public.contacts (organization_id, wa_id, name) values (1, '573001112233', 'Ana') returning id into ct;

  -- Check del contacto: teléfono inválido
  begin
    insert into public.contacts (organization_id, wa_id) values (1, '+57 300');
    raise exception 'debía fallar wa_id inválido';
  exception when check_violation then null;
  end;

  insert into public.conversations (organization_id, contact_id, channel_id, created_at, last_message_at)
    values (1, ct, ch, t0, t0) returning id into cv;
  select count(*) into n from public.conversation_events where conversation_id = cv and event_type = 'created';
  assert n = 1, 'evento created';

  -- Mensajes: contadores, preview e idempotencia por wa_message_id
  insert into public.messages (organization_id, conversation_id, direction, sender_type, text, wa_message_id, created_at)
    values (1, cv, 'in', 'contact', 'Hola, ¿precio del Onix?', 'wamid.A', t0 + interval '1 minute');
  insert into public.messages (organization_id, conversation_id, direction, sender_type, text, created_at)
    values (1, cv, 'out', 'bot', 'Te ayudo', t0 + interval '2 minutes');
  select * into c from public.conversations where id = cv;
  assert c.message_count = 2 and c.inbound_count = 1 and c.unread_count = 1, 'contadores';
  assert c.last_message_preview = 'Te ayudo', 'preview';
  assert c.last_inbound_at = t0 + interval '1 minute', 'last_inbound_at';
  assert (select message_id from public.message_wa_ids where wa_message_id = 'wamid.A') is not null, 'wa id registrado';

  -- Handoff, asignación, primera respuesta (SLA)
  perform set_config('app.actor_type', 'bot', true);
  update public.conversations set status = 'human', handoff_reason = 'pidió asesor', handoff_at = t0 + interval '3 minutes'
    where id = cv;
  perform set_config('app.actor_type', 'agent', true);
  perform set_config('app.actor_agent_id', ag::text, true);
  update public.conversations set assigned_agent_id = ag where id = cv;
  insert into public.messages (organization_id, conversation_id, direction, sender_type, sender_agent_id, text, created_at)
    values (1, cv, 'out', 'agent', ag, 'Hola Ana, soy Luis', t0 + interval '5 minutes');
  select * into c from public.conversations where id = cv;
  assert c.first_response_at = t0 + interval '5 minutes', 'first_response_at';
  assert (select (payload->>'seconds')::int from public.conversation_events
          where conversation_id = cv and event_type = 'first_agent_response') = 120, 'segundos de primera respuesta';
  assert (select actor_type from public.conversation_events where conversation_id = cv and event_type = 'handoff') = 'bot',
    'actor del handoff';
  assert (select actor_agent_id from public.conversation_events where conversation_id = cv and event_type = 'assigned') = ag,
    'actor de la asignación';

  -- Etiquetas y sugerencias
  insert into public.tags (organization_id, name, ai_description) values (1, 'cotizacion', 'pide precio') returning id into tg;
  begin
    insert into public.tags (organization_id, name) values (1, 'Mayúscula');
    raise exception 'debía fallar etiqueta no normalizada';
  exception when check_violation then null;
  end;
  insert into public.conversation_tags (conversation_id, tag_id, source, confidence) values (cv, tg, 'ai', 0.9);
  assert exists (select 1 from public.conversation_events where conversation_id = cv and event_type = 'tagged'), 'evento tagged';
  insert into public.conversation_suggestions (organization_id, conversation_id, kind, value, confidence)
    values (1, cv, 'typification', '"Venta"', 0.8);

  -- Campos personalizados: exactamente un valor tipado
  insert into public.contact_fields (organization_id, key, label, type) values (1, 'presupuesto', 'Presupuesto', 'number')
    returning id into f_num;
  insert into public.contact_field_values (contact_id, field_id, value_number, source) values (ct, f_num, 80000000, 'ai');
  begin
    update public.contact_field_values set value_text = 'x' where contact_id = ct and field_id = f_num;
    raise exception 'debía fallar con dos valores';
  exception when check_violation then null;
  end;

  -- Cierre con tipificación de éxito
  select id into venta from public.typifications where organization_id = 1 and name = 'Venta';
  update public.conversations set status = 'closed', typification_id = venta, ai_typification_id = venta where id = cv;
  select * into c from public.conversations where id = cv;
  assert c.closed_at is not null and c.unread_count = 0, 'cierre';
  assert (select status from public.conversation_suggestions where conversation_id = cv) = 'expired', 'sugerencias expiradas';
  assert (select (payload->>'had_handoff')::boolean from public.conversation_events
          where conversation_id = cv and event_type = 'closed') is true, 'had_handoff';

  -- Rollups
  perform reporting.refresh_range(1, (now() at time zone 'America/Bogota')::date - 1, (now() at time zone 'America/Bogota')::date);
  select sum(new_conversations) into n from reporting.daily_conversations where organization_id = 1;
  assert n = 1, 'rollup nuevas';
  assert (select sum(sales) from reporting.daily_conversations where organization_id = 1) = 1, 'rollup ventas';
  assert (select sum(first_response_within_sla) from reporting.daily_conversations where organization_id = 1) = 1, 'rollup SLA';
  assert (select sum(inbound_messages) from reporting.daily_conversations where organization_id = 1) = 1, 'rollup mensajes';
  assert (select sum(closed) from reporting.daily_agents where agent_id = ag) = 1, 'rollup asesor';
  assert (select sum(ai_agreed) from reporting.daily_typifications where organization_id = 1) = 1, 'acierto IA';
  assert (select sum(conversations) from reporting.daily_tags where tag_id = tg) = 1, 'rollup etiquetas';
  -- Idempotente
  perform reporting.refresh_range(1, (now() at time zone 'America/Bogota')::date - 1, (now() at time zone 'America/Bogota')::date);
  assert (select sum(new_conversations) from reporting.daily_conversations where organization_id = 1) = 1, 'idempotencia';

  -- Reapertura
  update public.conversations set status = 'bot' where id = cv;
  select * into c from public.conversations where id = cv;
  assert c.closed_at is null and c.typification_id is null and c.reopened_count = 1 and c.first_response_at is null, 'reapertura';

  -- Particiones del mes actual y retención
  assert to_regclass('public.messages_' || to_char(now(), 'YYYYMM')) is not null, 'partición del mes';
  execute 'create table public.ai_calls_200001 partition of public.ai_calls for values from (''2000-01-01'') to (''2000-02-01'')';
  assert public.drop_old_partitions('public.ai_calls', 6) = 1, 'retención';

  raise notice 'OK: comportamiento del modelo verificado';
end $$;

-- Realtime (stub): los triggers publican en los canales de la organización
do $$
begin
  assert exists (select 1 from realtime.messages where topic = 'org:1:inbox'), 'broadcast inbox';
  assert exists (select 1 from realtime.messages where topic like 'org:1:conversation:%'), 'broadcast conversación';
  raise notice 'OK: realtime';
end $$;

-- RLS: un agente autenticado solo ve su organización
insert into public.organizations (id, name, slug) values (2, 'Otra', 'otra');
insert into public.contacts (organization_id, wa_id, name) values (2, '573009998877', 'Ajeno');
grant usage on schema public to authenticated;
grant select on all tables in schema public to authenticated;
set role authenticated;
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-000000000001', false);
do $$
begin
  assert (select count(*) from public.contacts where organization_id = 2) = 0, 'RLS: no ve otra organización';
  assert (select count(*) from public.contacts where organization_id = 1) = 1, 'RLS: ve la suya';
  assert (select count(*) from public.ai_connections) = 0, 'RLS: configuración sensible solo admin';
  raise notice 'OK: RLS';
end $$;
reset role;
