-- =============================================================================
-- 20b · Origen "api" (docs/data-model.md §12.4)
-- Lo que entra por la API pública queda atribuido a la API: eventos de conversación (actor), negocios y
-- etiquetas de contacto (origen). contact_changes / contact_field_values ya aceptaban 'api'.
-- =============================================================================

alter table public.conversation_events drop constraint conversation_events_actor_type_check;
alter table public.conversation_events add constraint conversation_events_actor_type_check check (actor_type in
  ('system', 'bot', 'agent', 'ai', 'contact', 'automation', 'flow', 'api'));

alter table public.deals drop constraint deals_source_check;
alter table public.deals add constraint deals_source_check check (source in
  ('agent', 'ai', 'flow', 'crm', 'import', 'api'));

alter table public.contact_tags drop constraint contact_tags_source_check;
alter table public.contact_tags add constraint contact_tags_source_check check (source in
  ('agent', 'ai', 'import', 'rule', 'flow', 'api'));
