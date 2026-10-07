-- =============================================================================
-- 08 · Reporting: rollups diarios/horarios por organización (en su zona horaria)
-- Fuentes: conversation_events (hechos), messages, conversations, conversation_tags, ai_calls.
-- reporting.refresh_range() es idempotente: borra y recalcula el rango.
-- =============================================================================

create table reporting.daily_conversations (
  organization_id            bigint not null,
  day                        date not null,
  new_conversations          int not null default 0,
  ad_conversations           int not null default 0,
  handoffs                   int not null default 0,
  reopened                   int not null default 0,
  closed                     int not null default 0,
  closed_bot_only            int not null default 0,
  sales                      int not null default 0,
  first_responses            int not null default 0,
  first_response_sum_s       bigint not null default 0,
  first_response_within_sla  int not null default 0,
  inbound_messages           int not null default 0,
  bot_messages               int not null default 0,
  agent_messages             int not null default 0,
  campaign_messages          int not null default 0,
  flow_messages              int not null default 0,
  refreshed_at               timestamptz not null default now(),
  primary key (organization_id, day)
);

create table reporting.daily_agents (
  organization_id            bigint not null,
  day                        date not null,
  agent_id                   bigint not null,
  assigned                   int not null default 0,
  closed                     int not null default 0,
  sales                      int not null default 0,
  messages_sent              int not null default 0,
  first_responses            int not null default 0,
  first_response_sum_s       bigint not null default 0,
  first_response_within_sla  int not null default 0,
  primary key (organization_id, day, agent_id)
);

create table reporting.daily_groups (
  organization_id  bigint not null,
  day              date not null,
  group_id         bigint not null,
  routed_in        int not null default 0,
  closed           int not null default 0,
  sales            int not null default 0,
  primary key (organization_id, day, group_id)
);

create table reporting.daily_typifications (
  organization_id  bigint not null,
  day              date not null,
  typification_id  bigint not null,
  closed           int not null default 0,
  ai_compared      int not null default 0,       -- cerradas donde la IA también tipificó
  ai_agreed        int not null default 0,
  primary key (organization_id, day, typification_id)
);

create table reporting.daily_tags (
  organization_id  bigint not null,
  day              date not null,
  tag_id           bigint not null,
  conversations    int not null default 0,
  by_ai            int not null default 0,
  primary key (organization_id, day, tag_id)
);

create table reporting.hourly_messages (
  organization_id  bigint not null,
  hour             timestamptz not null,
  direction        text not null,
  sender_type      text not null,
  messages         int not null default 0,
  primary key (organization_id, hour, direction, sender_type)
);

create table reporting.daily_ai (
  organization_id  bigint not null,
  day              date not null,
  connection_id    bigint not null default 0,    -- 0 = sin conexión registrada
  purpose          text not null,
  calls            int not null default 0,
  errors           int not null default 0,       -- error + timeout + invalid + refused
  slow             int not null default 0,
  fallbacks        int not null default 0,
  latency_p50_ms   int,
  latency_p95_ms   int,
  input_tokens     bigint not null default 0,
  output_tokens    bigint not null default 0,
  cost_usd         numeric(14, 6) not null default 0,
  primary key (organization_id, day, connection_id, purpose)
);

create table reporting.daily_billing (
  organization_id   bigint not null,
  day               date not null,
  pricing_category  text not null,
  messages          int not null default 0,
  billable          int not null default 0,
  primary key (organization_id, day, pricing_category)
);

-- ---------------------------------------------------------------------------
create or replace function reporting.refresh_range(p_org bigint, p_from date, p_to date) returns void
language plpgsql as $$
declare
  tz  text := (select timezone from public.organizations where id = p_org);
  lo  timestamptz := (p_from::timestamp at time zone tz);
  hi  timestamptz := ((p_to + 1)::timestamp at time zone tz);
  sla int := coalesce((select (value->>'sla_minutes')::int from public.org_settings
                       where organization_id = p_org and key = 'conversations'), 5) * 60;
begin
  -- Conversaciones (desde hechos) --------------------------------------------
  delete from reporting.daily_conversations where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_conversations as d (organization_id, day, new_conversations, ad_conversations, handoffs,
      reopened, closed, closed_bot_only, sales, first_responses, first_response_sum_s, first_response_within_sla)
  select p_org, (e.occurred_at at time zone tz)::date,
         count(*) filter (where e.event_type = 'created'),
         count(*) filter (where e.event_type = 'created' and c.ad_source_id is not null),
         count(*) filter (where e.event_type = 'handoff'),
         count(*) filter (where e.event_type = 'reopened'),
         count(*) filter (where e.event_type = 'closed'),
         count(*) filter (where e.event_type = 'closed' and not coalesce((e.payload->>'had_handoff')::boolean, false)),
         count(*) filter (where e.event_type = 'closed' and t.is_success),
         count(*) filter (where e.event_type = 'first_agent_response'),
         coalesce(sum((e.payload->>'seconds')::bigint) filter (where e.event_type = 'first_agent_response'), 0),
         count(*) filter (where e.event_type = 'first_agent_response' and (e.payload->>'seconds')::int <= sla)
  from public.conversation_events e
  join public.conversations c on c.id = e.conversation_id
  left join public.typifications t on t.id = (e.payload->>'typification_id')::bigint
  where e.organization_id = p_org and e.occurred_at >= lo and e.occurred_at < hi
  group by 2;

  insert into reporting.daily_conversations as d (organization_id, day, inbound_messages, bot_messages, agent_messages,
      campaign_messages, flow_messages)
  select p_org, (m.created_at at time zone tz)::date,
         count(*) filter (where m.direction = 'in'),
         count(*) filter (where m.sender_type = 'bot'),
         count(*) filter (where m.sender_type = 'agent'),
         count(*) filter (where m.sender_type = 'campaign'),
         count(*) filter (where m.sender_type = 'flow')
  from public.messages m
  where m.organization_id = p_org and m.created_at >= lo and m.created_at < hi
  group by 2
  on conflict (organization_id, day) do update set
    inbound_messages = excluded.inbound_messages, bot_messages = excluded.bot_messages,
    agent_messages = excluded.agent_messages, campaign_messages = excluded.campaign_messages,
    flow_messages = excluded.flow_messages, refreshed_at = now();

  -- Asesores -------------------------------------------------------------------
  delete from reporting.daily_agents where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_agents (organization_id, day, agent_id, assigned, closed, sales,
      first_responses, first_response_sum_s, first_response_within_sla)
  select p_org, (e.occurred_at at time zone tz)::date, e.assigned_agent_id,
         count(*) filter (where e.event_type = 'assigned'),
         count(*) filter (where e.event_type = 'closed'),
         count(*) filter (where e.event_type = 'closed' and t.is_success),
         count(*) filter (where e.event_type = 'first_agent_response'),
         coalesce(sum((e.payload->>'seconds')::bigint) filter (where e.event_type = 'first_agent_response'), 0),
         count(*) filter (where e.event_type = 'first_agent_response' and (e.payload->>'seconds')::int <= sla)
  from public.conversation_events e
  left join public.typifications t on t.id = (e.payload->>'typification_id')::bigint
  where e.organization_id = p_org and e.occurred_at >= lo and e.occurred_at < hi
    and e.assigned_agent_id is not null
  group by 2, 3;

  insert into reporting.daily_agents (organization_id, day, agent_id, messages_sent)
  select p_org, (m.created_at at time zone tz)::date, m.sender_agent_id, count(*)
  from public.messages m
  where m.organization_id = p_org and m.created_at >= lo and m.created_at < hi
    and m.sender_type = 'agent' and m.sender_agent_id is not null
  group by 2, 3
  on conflict (organization_id, day, agent_id) do update set messages_sent = excluded.messages_sent;

  -- Grupos -------------------------------------------------------------------------
  delete from reporting.daily_groups where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_groups (organization_id, day, group_id, routed_in, closed, sales)
  select p_org, x.day, x.group_id, sum(x.routed), sum(x.closed), sum(x.sales)
  from (
    select (e.occurred_at at time zone tz)::date as day,
           case when e.event_type = 'routed' then (e.payload->>'to')::bigint else e.group_id end as group_id,
           (e.event_type = 'routed')::int as routed,
           (e.event_type = 'closed')::int as closed,
           (e.event_type = 'closed' and coalesce(t.is_success, false))::int as sales
    from public.conversation_events e
    left join public.typifications t on t.id = (e.payload->>'typification_id')::bigint
    where e.organization_id = p_org and e.occurred_at >= lo and e.occurred_at < hi
      and e.event_type in ('routed', 'closed')
  ) x
  where x.group_id is not null
  group by x.day, x.group_id;

  -- Tipificaciones y acierto de la IA -----------------------------------------
  delete from reporting.daily_typifications where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_typifications (organization_id, day, typification_id, closed, ai_compared, ai_agreed)
  select p_org, (c.closed_at at time zone tz)::date, c.typification_id, count(*),
         count(*) filter (where c.ai_typification_id is not null),
         count(*) filter (where c.ai_typification_id = c.typification_id)
  from public.conversations c
  where c.organization_id = p_org and c.closed_at >= lo and c.closed_at < hi and c.typification_id is not null
  group by 2, 3;

  -- Etiquetas ----------------------------------------------------------------------
  delete from reporting.daily_tags where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_tags (organization_id, day, tag_id, conversations, by_ai)
  select p_org, (ct.created_at at time zone tz)::date, ct.tag_id, count(*), count(*) filter (where ct.source = 'ai')
  from public.conversation_tags ct
  join public.tags tg on tg.id = ct.tag_id and tg.organization_id = p_org
  where ct.created_at >= lo and ct.created_at < hi
  group by 2, 3;

  -- Mensajes por hora ----------------------------------------------------------------
  delete from reporting.hourly_messages where organization_id = p_org and hour >= lo and hour < hi;
  insert into reporting.hourly_messages (organization_id, hour, direction, sender_type, messages)
  select p_org, date_trunc('hour', m.created_at), m.direction, m.sender_type, count(*)
  from public.messages m
  where m.organization_id = p_org and m.created_at >= lo and m.created_at < hi
  group by 2, 3, 4;

  -- IA (salud, latencia, costo, failover) ------------------------------------------
  delete from reporting.daily_ai where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_ai (organization_id, day, connection_id, purpose, calls, errors, slow, fallbacks,
      latency_p50_ms, latency_p95_ms, input_tokens, output_tokens, cost_usd)
  select p_org, (a.created_at at time zone tz)::date, coalesce(a.connection_id, 0), a.purpose, count(*),
         count(*) filter (where a.status in ('error', 'timeout', 'invalid', 'refused')),
         count(*) filter (where a.status = 'slow'),
         count(*) filter (where a.fallback_from_call_id is not null),
         percentile_cont(0.5) within group (order by a.latency_ms)::int,
         percentile_cont(0.95) within group (order by a.latency_ms)::int,
         coalesce(sum(a.input_tokens), 0), coalesce(sum(a.output_tokens), 0), coalesce(sum(a.cost_usd), 0)
  from public.ai_calls a
  where a.organization_id = p_org and a.created_at >= lo and a.created_at < hi
  group by 2, 3, 4;

  -- Facturación de Meta --------------------------------------------------------------
  delete from reporting.daily_billing where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_billing (organization_id, day, pricing_category, messages, billable)
  select p_org, (m.created_at at time zone tz)::date, m.pricing_category, count(*), count(*) filter (where m.billable)
  from public.messages m
  where m.organization_id = p_org and m.created_at >= lo and m.created_at < hi and m.pricing_category is not null
  group by 2, 3;
end $$;

-- Hoy y ayer para todas las organizaciones (lo agenda pg_cron cada 10 minutos)
create or replace function reporting.refresh_recent() returns void
language plpgsql as $$
declare
  o record;
begin
  for o in select id, timezone from public.organizations loop
    perform reporting.refresh_range(o.id, (now() at time zone o.timezone)::date - 1, (now() at time zone o.timezone)::date);
  end loop;
end $$;

-- Vistas de lectura para la API (por rango de días)
create or replace view reporting.v_agent_sla as
select a.organization_id, a.day, a.agent_id, ag.name as agent_name, a.closed, a.sales, a.messages_sent,
       a.first_responses,
       case when a.first_responses > 0 then round(a.first_response_sum_s::numeric / a.first_responses / 60, 1) end
         as avg_first_response_min,
       case when a.first_responses > 0 then round(100.0 * a.first_response_within_sla / a.first_responses, 1) end
         as sla_pct
from reporting.daily_agents a
join public.agents ag on ag.id = a.agent_id;
