-- =============================================================================
-- 21b · Propósito de IA del asistente de onboarding (docs/data-model.md §13)
-- La importación del sitio web y la reescritura de plantillas rechazadas quedan en ai_calls con su propósito.
-- =============================================================================

alter table public.ai_calls drop constraint ai_calls_purpose_check;
alter table public.ai_calls add constraint ai_calls_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'test', 'qa', 'agent_test', 'onboarding'));

alter table public.cortexes drop constraint cortexes_purpose_check;
alter table public.cortexes add constraint cortexes_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'qa', 'agent_test', 'onboarding', 'any'));
