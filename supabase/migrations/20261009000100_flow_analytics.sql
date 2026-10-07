-- =============================================================================
-- 15 · Análisis de flujos (Reportes → Análisis de flujos)
-- Embudo por bloque: cuántas ejecuciones llegan a cada bloque, dónde abandonan, errores, latencia y
-- qué opción eligen los clientes en los bloques de decisión.
--
-- 1) flow_run_steps se desnormaliza con organization_id y flow_id: el reporte agrupa millones de pasos
--    por flujo sin unir con flow_runs (que vive en otra partición por started_at).
-- 2) Tres rollups diarios en reporting.*, recalculados por reporting.refresh_range (idempotente).
-- =============================================================================

-- 1) Desnormalización ----------------------------------------------------------------------------
alter table public.flow_run_steps add column organization_id bigint, add column flow_id bigint;

update public.flow_run_steps s set organization_id = r.organization_id, flow_id = r.flow_id
from public.flow_runs r
where r.id = s.run_id and r.started_at = s.run_started_at and s.organization_id is null;

alter table public.flow_run_steps alter column organization_id set not null, alter column flow_id set not null;

-- El motor ya los envía; el trigger cubre cualquier otro escritor (falla cerrado si la ejecución no existe).
create or replace function private.flow_run_steps_fill() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if new.organization_id is null or new.flow_id is null then
    select r.organization_id, r.flow_id into new.organization_id, new.flow_id
    from public.flow_runs r where r.id = new.run_id and r.started_at = new.run_started_at;
  end if;
  return new;
end $$;
revoke all on function private.flow_run_steps_fill() from public;

create trigger trg_flow_run_steps_fill before insert on public.flow_run_steps
for each row execute function private.flow_run_steps_fill();

create index flow_run_steps_flow_idx on public.flow_run_steps (organization_id, flow_id, created_at);

-- RLS: los pasos ya se pueden leer por organización (antes no tenían política = sin acceso).
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'auth') then
    execute 'create policy org_read on public.flow_run_steps for select to authenticated
             using (organization_id = private.current_org_id())';
  end if;
end $$;

-- 2) Rollups -----------------------------------------------------------------------------------
create table reporting.daily_flows (
  organization_id  bigint not null,
  day              date not null,
  flow_id          bigint not null,
  started          int not null default 0,
  succeeded        int not null default 0,
  failed           int not null default 0,
  cancelled        int not null default 0,
  waiting          int not null default 0,     -- ejecuciones iniciadas ese día que siguen esperando
  duration_sum_s   bigint not null default 0,  -- solo terminadas
  finished         int not null default 0,
  refreshed_at     timestamptz not null default now(),
  primary key (organization_id, day, flow_id)
);

create table reporting.daily_flow_blocks (
  organization_id  bigint not null,
  day              date not null,
  flow_id          bigint not null,
  block_id         text not null,
  block_type       text not null,
  runs             int not null default 0,     -- ejecuciones distintas que llegaron al bloque
  executions       int not null default 0,     -- pasos (un bloque en un bucle cuenta varias veces)
  errors           int not null default 0,
  waits            int not null default 0,
  exits            int not null default 0,     -- ejecuciones fallidas/canceladas cuyo último paso fue este bloque
  stuck            int not null default 0,     -- ejecuciones que siguen esperando en este bloque
  latency_p50_ms   int,
  latency_p95_ms   int,
  refreshed_at     timestamptz not null default now(),
  primary key (organization_id, day, flow_id, block_id)
);

create table reporting.daily_flow_choices (
  organization_id  bigint not null,
  day              date not null,
  flow_id          bigint not null,
  block_id         text not null,
  choice           text not null,              -- etiqueta de la opción u "other"
  runs             int not null default 0,
  primary key (organization_id, day, flow_id, block_id, choice)
);

create index daily_flow_blocks_flow_idx on reporting.daily_flow_blocks (organization_id, flow_id, day);
create index daily_flow_choices_flow_idx on reporting.daily_flow_choices (organization_id, flow_id, day);

create or replace function reporting.refresh_flows(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
declare
  tz  text := (select timezone from public.organizations where id = p_org);
  lo  timestamptz := (p_from::timestamp at time zone tz);
  hi  timestamptz := ((p_to + 1)::timestamp at time zone tz);
begin
  -- Ejecuciones (por día de inicio) -----------------------------------------------------------
  delete from reporting.daily_flows where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_flows (organization_id, day, flow_id, started, succeeded, failed, cancelled, waiting,
      duration_sum_s, finished)
  select p_org, (r.started_at at time zone tz)::date, r.flow_id, count(*),
         count(*) filter (where r.status = 'succeeded'),
         count(*) filter (where r.status = 'failed'),
         count(*) filter (where r.status = 'cancelled'),
         count(*) filter (where r.status in ('running', 'waiting')),
         coalesce(sum(extract(epoch from r.finished_at - r.started_at)::bigint) filter (where r.finished_at is not null), 0),
         count(*) filter (where r.finished_at is not null)
  from public.flow_runs r
  where r.organization_id = p_org and r.started_at >= lo and r.started_at < hi
  group by 2, 3;

  -- Bloques (por día del paso) ----------------------------------------------------------------
  delete from reporting.daily_flow_blocks where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_flow_blocks (organization_id, day, flow_id, block_id, block_type, runs, executions,
      errors, waits, exits, stuck, latency_p50_ms, latency_p95_ms)
  with steps as (
    select s.*, (s.created_at at time zone tz)::date as day,
           row_number() over (partition by s.run_id order by s.created_at desc, s.id desc) as rn_last
    from public.flow_run_steps s
    where s.organization_id = p_org and s.created_at >= lo and s.created_at < hi
  )
  select p_org, st.day, st.flow_id, st.block_id, max(st.block_type), count(distinct st.run_id), count(*),
         count(*) filter (where st.status = 'error'),
         count(*) filter (where st.status = 'waiting'),
         count(distinct st.run_id) filter (where st.rn_last = 1 and r.status in ('failed', 'cancelled')),
         count(distinct st.run_id) filter (where st.rn_last = 1 and r.status = 'waiting'),
         percentile_cont(0.5) within group (order by st.latency_ms)::int,
         percentile_cont(0.95) within group (order by st.latency_ms)::int
  from steps st
  join public.flow_runs r on r.id = st.run_id and r.started_at = st.run_started_at
  group by 2, 3, 4;

  -- Opciones elegidas -------------------------------------------------------------------------
  delete from reporting.daily_flow_choices where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_flow_choices (organization_id, day, flow_id, block_id, choice, runs)
  select p_org, (s.created_at at time zone tz)::date, s.flow_id, s.block_id, s.output->>'choice',
         count(distinct s.run_id)
  from public.flow_run_steps s
  where s.organization_id = p_org and s.created_at >= lo and s.created_at < hi
    and s.status = 'ok' and s.output ? 'choice'
  group by 2, 3, 4, 5;
end $$;

-- refresh_range (la API pública del reporting) pasa a incluir los flujos sin copiar su cuerpo.
alter function reporting.refresh_range(bigint, date, date) rename to refresh_core;

create or replace function reporting.refresh_range(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
begin
  perform reporting.refresh_core(p_org, p_from, p_to);
  perform reporting.refresh_flows(p_org, p_from, p_to);
end $$;

revoke all on function reporting.refresh_flows(bigint, date, date) from public;
