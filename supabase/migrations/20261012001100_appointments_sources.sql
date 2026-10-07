-- =============================================================================
-- 25b · Citas creadas por webhooks entrantes y por la API (docs/data-model.md §17)
-- =============================================================================
alter table public.appointments drop constraint appointments_created_by_type_check;
alter table public.appointments add constraint appointments_created_by_type_check
  check (created_by_type in ('bot', 'agent', 'flow', 'api', 'webhook'));
