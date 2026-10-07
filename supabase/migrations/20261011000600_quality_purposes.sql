-- =============================================================================
-- 22 · Propósitos de IA de calidad (docs/data-model.md §12.3)
-- Las revisiones de conversaciones (qa) y las pruebas de agentes (agent_test) quedan en ai_calls con su
-- propósito (costo y salud separados en Reportes → IA y Cortex) y pueden tener su propio Cortex.
-- =============================================================================

alter table public.ai_calls drop constraint ai_calls_purpose_check;
alter table public.ai_calls add constraint ai_calls_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'test', 'qa', 'agent_test'));

alter table public.cortexes drop constraint cortexes_purpose_check;
alter table public.cortexes add constraint cortexes_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'qa', 'agent_test', 'any'));
