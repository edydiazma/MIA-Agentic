-- =============================================================================
-- 23b · Propósito de IA del registro maestro (docs/data-model.md §15)
-- La lectura de documentos (cédula, tarjeta de propiedad, SOAT…), la extracción de llaves al cerrar y el asistente
-- de consolidación de campos quedan en ai_calls con su propósito y pueden tener su propio Cortex.
-- =============================================================================

alter table public.ai_calls drop constraint ai_calls_purpose_check;
alter table public.ai_calls add constraint ai_calls_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'test', 'qa', 'agent_test', 'onboarding', 'golden'));

alter table public.cortexes drop constraint cortexes_purpose_check;
alter table public.cortexes add constraint cortexes_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'qa', 'agent_test', 'onboarding', 'golden', 'any'));
