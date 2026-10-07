-- =============================================================================
-- 27 · Supervisión y KPIs de servicio (docs/data-model.md §18.2)
--
-- Supervisor con alcance a sus grupos (agent_groups.role), KPIs históricos (AHT, abandono, no atendidas, tasa de
-- atención, clientes únicos vs casos, solo-bot vs asesor) en reporting.daily_service, y tiempo por estado de
-- asesor (reporte "Login") en reporting.daily_agent_status.
-- =============================================================================

alter table public.agent_groups
  add column role text not null default 'member' check (role in ('member', 'supervisor'));
create index agent_groups_supervisor_idx on public.agent_groups (agent_id) where role = 'supervisor';

-- Supervisores existentes supervisan los grupos a los que ya pertenecían
update public.agent_groups ag set role = 'supervisor'
from public.agents a where a.id = ag.agent_id and a.role = 'supervisor';

create table reporting.daily_service (
  organization_id     bigint not null,
  day                 date not null,
  group_id            bigint not null,          -- 0 = sin grupo
  agent_id            bigint not null,          -- 0 = sin asesor
  cases               int not null default 0,   -- conversaciones creadas
  unique_contacts     int not null default 0,
  returning_cases     int not null default 0,
  bot_only            int not null default 0,   -- cerradas sin transferencia
  handoffs            int not null default 0,
  attended            int not null default 0,   -- transferidas con respuesta de asesor
  not_attended        int not null default 0,   -- transferidas cerradas sin respuesta de asesor
  abandoned           int not null default 0,   -- el cliente se fue en cola (cerró por inactividad sin respuesta)
  reassigned          int not null default 0,   -- más de una asignación
  closed              int not null default 0,
  aht_sum_s           bigint not null default 0, -- asignación → cierre
  aht_count           int not null default 0,
  wait_sum_s          bigint not null default 0, -- transferencia → primera respuesta
  wait_count          int not null default 0,
  refreshed_at        timestamptz not null default now(),
  primary key (organization_id, day, group_id, agent_id)
);

create table reporting.daily_agent_status (
  organization_id  bigint not null,
  day              date not null,
  agent_id         bigint not null,
  status_key       text not null,
  seconds          bigint not null default 0,
  refreshed_at     timestamptz not null default now(),
  primary key (organization_id, day, agent_id, status_key)
);

create or replace function reporting.refresh_service(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
declare
  tz  text := (select timezone from public.organizations where id = p_org);
  lo  timestamptz := (p_from::timestamp at time zone tz);
  hi  timestamptz := ((p_to + 1)::timestamp at time zone tz);
begin
  delete from reporting.daily_service where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_service (organization_id, day, group_id, agent_id, cases, unique_contacts, returning_cases,
      bot_only, handoffs, attended, not_attended, abandoned, reassigned, closed, aht_sum_s, aht_count, wait_sum_s, wait_count)
  select p_org, (c.created_at at time zone tz)::date, coalesce(c.group_id, 0), coalesce(c.assigned_agent_id, 0),
         count(*), count(distinct c.contact_id), count(*) filter (where c.is_returning),
         count(*) filter (where c.closed_at is not null and c.handoff_at is null),
         count(*) filter (where c.handoff_at is not null),
         count(*) filter (where c.handoff_at is not null and c.first_response_at is not null),
         count(*) filter (where c.handoff_at is not null and c.first_response_at is null and c.closed_at is not null),
         count(*) filter (where c.handoff_at is not null and c.first_response_at is null and c.closed_at is not null
                          and c.assigned_agent_id is null),
         count(*) filter (where c.assignment_count > 1),
         count(*) filter (where c.closed_at is not null),
         coalesce(sum(extract(epoch from c.closed_at - c.first_assigned_at)::bigint)
                  filter (where c.closed_at > c.first_assigned_at), 0),
         count(*) filter (where c.closed_at > c.first_assigned_at),
         coalesce(sum(extract(epoch from c.first_response_at - c.handoff_at)::bigint)
                  filter (where c.first_response_at >= c.handoff_at), 0),
         count(*) filter (where c.first_response_at >= c.handoff_at)
  from public.conversations c
  where c.organization_id = p_org and c.created_at >= lo and c.created_at < hi
  group by 2, 3, 4;

  -- Tiempo por estado: cada tramo se reparte por día local (un tramo puede cruzar la medianoche)
  delete from reporting.daily_agent_status where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_agent_status (organization_id, day, agent_id, status_key, seconds)
  select p_org, d.day, e.agent_id, e.status_key,
         sum(extract(epoch from least(coalesce(e.ended_at, now()), ((d.day + 1)::timestamp at time zone tz))
                               - greatest(e.started_at, (d.day::timestamp at time zone tz)))::bigint)
  from public.agent_status_events e
  cross join lateral generate_series((greatest(e.started_at, lo) at time zone tz)::date,
                                     (least(coalesce(e.ended_at, now()), hi) at time zone tz)::date, interval '1 day') g(dd)
  cross join lateral (select g.dd::date as day) d
  where e.organization_id = p_org and e.started_at < hi and coalesce(e.ended_at, now()) > lo
    and d.day between p_from and p_to
  group by 2, 3, 4
  having sum(extract(epoch from least(coalesce(e.ended_at, now()), ((d.day + 1)::timestamp at time zone tz))
                               - greatest(e.started_at, (d.day::timestamp at time zone tz)))) > 0;
end $$;
revoke all on function reporting.refresh_service(bigint, date, date) from public;

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
  perform reporting.refresh_ads(p_org, p_from, p_to);
  perform reporting.refresh_service(p_org, p_from, p_to);
end $$;
