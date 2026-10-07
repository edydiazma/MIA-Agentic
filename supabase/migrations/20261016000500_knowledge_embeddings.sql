-- =============================================================================
-- 35b · Embeddings de la base de conocimiento (docs/data-model.md §21.2)
--
-- Las conexiones de IA admiten el proveedor 'voyage' (Voyage AI: embeddings y rerank) y los propósitos de IA
-- incluyen 'embedding' (costo de indexar y consultar queda en ai_calls como cualquier otra llamada).
-- Listas copiadas de la última versión (20261014000300_copilot.sql) + los valores nuevos.
-- =============================================================================

alter table public.ai_connections drop constraint ai_connections_provider_check;
alter table public.ai_connections add constraint ai_connections_provider_check
  check (provider in ('anthropic', 'openai', 'openai_compatible', 'azure_openai', 'voyage'));

alter table public.ai_calls drop constraint ai_calls_purpose_check;
alter table public.ai_calls add constraint ai_calls_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'test', 'qa', 'agent_test', 'onboarding', 'golden',
   'copilot', 'assistant', 'embedding'));
alter table public.cortexes drop constraint cortexes_purpose_check;
alter table public.cortexes add constraint cortexes_purpose_check check (purpose in
  ('chat', 'classification', 'learning', 'flow', 'json_edit', 'qa', 'agent_test', 'onboarding', 'golden', 'copilot',
   'assistant', 'embedding', 'any'));
