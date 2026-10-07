-- =============================================================================
-- 18b · Omnicanal: los webhooks crudos de Messenger e Instagram también quedan en inbound_events
-- (docs/data-model.md §12.2)
-- =============================================================================
alter table public.inbound_events drop constraint if exists inbound_events_source_check;
alter table public.inbound_events add constraint inbound_events_source_check
  check (source in ('whatsapp', 'messenger', 'instagram', 'flow_webhook', 'catalog_feed', 'hubspot', 'salesforce',
                    'stripe'));
