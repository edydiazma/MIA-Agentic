-- =============================================================================
-- 28b · Disparadores de flujo: «Cuando llega por un mensaje disparador» (wa_link, §10.5) y
--       «Cuando se tipifica» (typified, §18.3). flows.trigger_type no los admitía.
-- =============================================================================
alter table public.flows drop constraint if exists flows_trigger_type_check;
alter table public.flows add constraint flows_trigger_type_check check (trigger_type in
  ('inbound_message', 'keyword', 'handoff', 'close', 'schedule', 'webhook', 'manual', 'campaign_reply',
   'wa_link', 'typified'));
