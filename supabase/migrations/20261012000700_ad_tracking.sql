-- =============================================================================
-- 24 · Rastreo por anuncio y fuente: creativo, publicación, fuente del cliente e inversión (docs/data-model.md §16)
--
-- El referral de Click to WhatsApp trae source_type (ad | post), source_id, source_url (ej.
-- https://www.facebook.com/story.php?story_fbid=1060896133661462&id=100092232559076 o .../posts/...), headline,
-- body, media_type, image_url / video_url / thumbnail_url y ctwa_clid. Un anuncio hecho sobre una publicación
-- tiene creative.effective_object_story_id = "{page_id}_{post_id}": así una visita desde la publicación se cruza
-- con el anuncio exacto que la promocionó.
-- El cliente guarda su PRIMERA y su ÚLTIMA fuente (canal, anuncio, campaña) para la lista y BI.
-- ad_spend_daily: inversión por anuncio y día desde Meta Insights y Google Ads → costo por lead, por venta, ROAS.
-- =============================================================================

-- Detalle del toque: creativo y publicación -------------------------------------------------------------
alter table public.attributions
  add column source_type     text,          -- ad | post | (Messenger/IG) ad | null
  add column page_id         text,
  add column post_id         text,          -- "{page_id}_{post_id}" (effective_object_story_id)
  add column ad_headline     text,
  add column ad_body         text,
  add column ad_media_type   text,          -- image | video
  add column ad_media_url    text,
  add column ad_thumbnail_url text,
  add column source_url      text;          -- URL original del referral (publicación o anuncio)
create index attributions_ad_idx on public.attributions (organization_id, ad_id, created_at) where ad_id is not null;
create index attributions_post_idx on public.attributions (organization_id, post_id) where post_id is not null;
create index attributions_gcampaign_idx on public.attributions (organization_id, platform_campaign_id, created_at)
  where platform_campaign_id is not null;

alter table public.attribution_touches
  add column source_type   text,
  add column post_id       text,
  add column campaign_id   text,
  add column campaign_name text,
  add column ad_name       text,
  add column ad_headline   text;

-- Caché de anuncios con su creativo y la publicación asociada
alter table public.ad_entities
  add column status                    text,     -- ACTIVE | PAUSED | ...
  add column effective_object_story_id text,     -- "{page_id}_{post_id}" de la publicación del anuncio
  add column headline                  text,
  add column body                      text,
  add column media_type                text,
  add column media_url                 text,
  add column thumbnail_url             text,
  add column destination               text,     -- whatsapp | messenger | instagram_direct | website
  add column account_id                text;
create index ad_entities_story_idx on public.ad_entities (organization_id, effective_object_story_id)
  where effective_object_story_id is not null;

-- Fuente del cliente (primer y último toque) ---------------------------------------------------------------
alter table public.contacts
  add column first_source_channel   text,     -- canal de atribución (meta_ctwa, google_ads, organic_web…)
  add column first_source_ad_id     text,
  add column first_source_campaign  text,     -- nombre de campaña (o utm_campaign)
  add column first_source_label     text,     -- texto listo para mostrar: "Meta · Promo CX-5 · Anuncio 3"
  add column first_source_at        timestamptz,
  add column first_attribution_id   bigint,   -- atribución del primer toque (se re-etiqueta al enriquecerse)
  add column last_source_channel    text,
  add column last_source_ad_id      text,
  add column last_source_campaign   text,
  add column last_source_label      text,
  add column last_source_at         timestamptz,
  add column last_attribution_id    bigint;
create index contacts_first_source_idx on public.contacts (organization_id, first_source_channel, first_source_at);
create index contacts_first_ad_idx on public.contacts (organization_id, first_source_ad_id) where first_source_ad_id is not null;
create index contacts_last_ad_idx on public.contacts (organization_id, last_source_ad_id) where last_source_ad_id is not null;

create or replace function private.attribution_label(a public.attributions) returns text
language sql stable set search_path = public, pg_temp as $$
  select nullif(concat_ws(' · ',
    case a.channel when 'meta_ctwa' then 'Meta' when 'google_ads' then 'Google Ads' when 'meta_ads_web' then 'Meta (web)'
                   when 'instagram' then 'Instagram' when 'messenger' then 'Messenger' when 'organic_web' then 'Web orgánica'
                   when 'paid_other' then 'Pago (otro)' when 'offline' then 'Offline' when 'campaign' then 'Campaña WhatsApp'
                   when 'webchat' then 'Chat web' else 'Directo' end,
    coalesce(a.platform_campaign_name, a.utm_campaign),
    coalesce(a.ad_name, case when a.source_type = 'post' then 'Publicación' end, a.ad_id)), '')
$$;
revoke all on function private.attribution_label(public.attributions) from public;

create or replace function private.trg_attributions_contact_source() returns trigger
language plpgsql set search_path = public, pg_temp as $$
declare
  v_label text := private.attribution_label(new);
  v_campaign text := coalesce(new.platform_campaign_name, new.utm_campaign);
  v_at timestamptz := coalesce(new.last_touch_at, new.created_at);
begin
  -- "directo" nunca reemplaza una fuente real
  if new.channel = 'direct' then
    update public.contacts set first_source_channel = 'direct', first_source_label = v_label,
                               first_source_at = new.created_at, first_attribution_id = new.id
    where id = new.contact_id and first_attribution_id is null;
    return null;
  end if;
  update public.contacts k set
    first_attribution_id  = new.id,
    first_source_channel  = new.channel,
    first_source_ad_id    = new.ad_id,
    first_source_campaign = v_campaign,
    first_source_label    = v_label,
    first_source_at       = new.created_at
  where k.id = new.contact_id
    and (k.first_attribution_id is null or k.first_attribution_id = new.id or k.first_source_channel = 'direct');
  update public.contacts k set
    last_attribution_id  = new.id,
    last_source_channel  = new.channel,
    last_source_ad_id    = new.ad_id,
    last_source_campaign = v_campaign,
    last_source_label    = v_label,
    last_source_at       = v_at
  where k.id = new.contact_id
    and (k.last_attribution_id is null or k.last_attribution_id = new.id or k.last_source_at is null
         or v_at >= k.last_source_at);
  return null;
end $$;
revoke all on function private.trg_attributions_contact_source() from public;
create trigger trg_attributions_contact_source after insert or update on public.attributions
  for each row execute function private.trg_attributions_contact_source();

-- Backfill
update public.contacts k set
  first_source_channel = f.channel, first_source_ad_id = f.ad_id,
  first_source_campaign = coalesce(f.platform_campaign_name, f.utm_campaign),
  first_source_label = private.attribution_label(f), first_source_at = f.created_at, first_attribution_id = f.id,
  last_source_channel = l.channel, last_source_ad_id = l.ad_id,
  last_source_campaign = coalesce(l.platform_campaign_name, l.utm_campaign),
  last_source_label = private.attribution_label(l), last_source_at = coalesce(l.last_touch_at, l.created_at),
  last_attribution_id = l.id
from public.contacts c
cross join lateral (select a.* from public.attributions a where a.contact_id = c.id
                    order by (a.channel = 'direct'), a.created_at limit 1) f
cross join lateral (select a.* from public.attributions a where a.contact_id = c.id
                    order by (a.channel = 'direct'), coalesce(a.last_touch_at, a.created_at) desc limit 1) l
where k.id = c.id;

-- Inversión publicitaria ------------------------------------------------------------------------------------
create table public.ad_spend_daily (
  organization_id   bigint not null references public.organizations(id) on delete cascade,
  platform          text not null check (platform in ('meta', 'google_ads')),
  day               date not null,                -- en la zona horaria de la cuenta publicitaria
  level             text not null check (level in ('ad', 'ad_group', 'campaign')),
  entity_id         text not null,                -- ad_id / ad_group_id / campaign_id según el nivel
  account_id        text,
  campaign_id       text,
  campaign_name     text,
  ad_group_id       text,
  ad_group_name     text,
  ad_id             text,
  ad_name           text,
  impressions       bigint not null default 0,
  clicks            bigint not null default 0,
  spend             numeric(14, 2) not null default 0,
  currency          char(3),
  platform_conversations bigint,                  -- Meta: conversaciones iniciadas según la plataforma
  fetched_at        timestamptz not null default now(),
  primary key (organization_id, platform, level, entity_id, day)
);
create index ad_spend_daily_campaign_idx on public.ad_spend_daily (organization_id, platform, campaign_id, day);
create index ad_spend_daily_day_idx on public.ad_spend_daily (organization_id, day);

-- Rendimiento por anuncio y día: nuestras conversaciones/ventas + inversión de la plataforma
create table reporting.daily_ads (
  organization_id   bigint not null,
  day               date not null,
  platform          text not null,              -- meta | google_ads | other
  ad_key            text not null,              -- ad_id, post:{id}, campaign:{id}, utm:{campaign}
  ad_id             text,
  ad_name           text,
  campaign_id       text,
  campaign_name     text,
  conversations     int not null default 0,     -- conversaciones atribuidas (último toque)
  new_contacts      int not null default 0,     -- clientes cuyo primer toque fue este anuncio
  sales             int not null default 0,     -- conversaciones cerradas con tipificación de venta
  conversions       int not null default 0,
  conversion_value  numeric(16, 2) not null default 0,
  deals_won_value   numeric(16, 2) not null default 0,
  impressions       bigint not null default 0,
  clicks            bigint not null default 0,
  spend             numeric(14, 2) not null default 0,
  refreshed_at      timestamptz not null default now(),
  primary key (organization_id, day, platform, ad_key)
);

create or replace function reporting.refresh_ads(p_org bigint, p_from date, p_to date) returns void
language plpgsql set search_path = reporting, public, pg_temp as $$
declare
  tz  text := (select timezone from public.organizations where id = p_org);
  lo  timestamptz := (p_from::timestamp at time zone tz);
  hi  timestamptz := ((p_to + 1)::timestamp at time zone tz);
begin
  delete from reporting.daily_ads where organization_id = p_org and day between p_from and p_to;
  insert into reporting.daily_ads (organization_id, day, platform, ad_key, ad_id, ad_name, campaign_id, campaign_name,
      conversations, new_contacts, sales, conversions, conversion_value, deals_won_value, impressions, clicks, spend)
  select p_org, x.day, x.platform, x.ad_key, max(x.ad_id), max(x.ad_name), max(x.campaign_id), max(x.campaign_name),
         sum(x.conversations), sum(x.new_contacts), sum(x.sales), sum(x.conversions), sum(x.conversion_value),
         sum(x.deals_won_value), sum(x.impressions), sum(x.clicks), sum(x.spend)
  from (
    -- conversaciones y ventas atribuidas
    select (a.created_at at time zone tz)::date as day,
           case when a.channel in ('meta_ctwa', 'meta_ads_web', 'instagram', 'messenger') then 'meta'
                when a.channel = 'google_ads' then 'google_ads' else 'other' end as platform,
           coalesce(a.ad_id, 'post:' || a.post_id, 'campaign:' || a.platform_campaign_id, 'utm:' || a.utm_campaign,
                    'channel:' || a.channel) as ad_key,
           a.ad_id, a.ad_name, a.platform_campaign_id as campaign_id,
           coalesce(a.platform_campaign_name, a.utm_campaign) as campaign_name,
           1 as conversations, 0 as new_contacts,
           (c.status = 'closed' and t.is_success)::int as sales,
           0 as conversions, 0::numeric as conversion_value, 0::numeric as deals_won_value,
           0::bigint as impressions, 0::bigint as clicks, 0::numeric as spend
    from public.attributions a
    join public.conversations c on c.id = a.conversation_id
    left join public.typifications t on t.id = c.typification_id
    where a.organization_id = p_org and a.created_at >= lo and a.created_at < hi
    union all
    -- clientes nuevos por su primer anuncio
    select (k.first_source_at at time zone tz)::date,
           case when k.first_source_channel in ('meta_ctwa', 'meta_ads_web', 'instagram', 'messenger') then 'meta'
                when k.first_source_channel = 'google_ads' then 'google_ads' else 'other' end,
           coalesce(k.first_source_ad_id, 'utm:' || k.first_source_campaign, 'channel:' || k.first_source_channel),
           k.first_source_ad_id, null, null, k.first_source_campaign, 0, 1, 0, 0, 0, 0, 0, 0, 0
    from public.contacts k
    where k.organization_id = p_org and k.first_source_at >= lo and k.first_source_at < hi
      and k.first_source_channel is not null
    union all
    -- conversiones (subidas o no) por el anuncio de su atribución
    select (e.occurred_at at time zone tz)::date,
           case when e.attribution->>'channel' in ('meta_ctwa', 'meta_ads_web', 'instagram', 'messenger') then 'meta'
                when e.attribution->>'channel' = 'google_ads' then 'google_ads' else 'other' end,
           coalesce(e.attribution->>'ad_id', 'campaign:' || (e.attribution->>'platform_campaign_id'),
                    'utm:' || (e.attribution->>'utm_campaign'), 'channel:' || coalesce(e.attribution->>'channel', 'direct')),
           e.attribution->>'ad_id', null, null, coalesce(e.attribution->>'platform_campaign_name',
           e.attribution->>'utm_campaign'), 0, 0, 0, 1, coalesce(e.value, 0), 0, 0, 0, 0
    from public.conversion_events e
    where e.organization_id = p_org and e.occurred_at >= lo and e.occurred_at < hi
    union all
    -- inversión (nivel anuncio de Meta; nivel campaña de Google si no hay detalle por anuncio)
    select s.day, s.platform,
           case when s.level = 'ad' then s.entity_id else 'campaign:' || s.campaign_id end,
           s.ad_id, s.ad_name, s.campaign_id, s.campaign_name, 0, 0, 0, 0, 0, 0, s.impressions, s.clicks, s.spend
    from public.ad_spend_daily s
    where s.organization_id = p_org and s.day between p_from and p_to
      and (s.level = 'ad' or (s.level = 'campaign' and not exists (
             select 1 from public.ad_spend_daily s2 where s2.organization_id = p_org and s2.platform = s.platform
               and s2.level = 'ad' and s2.campaign_id = s.campaign_id and s2.day = s.day)))
  ) x
  group by 2, 3, 4;
end $$;
revoke all on function reporting.refresh_ads(bigint, date, date) from public;

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
end $$;

alter table public.ad_spend_daily enable row level security;
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'auth') then
    execute 'create policy org_read on public.ad_spend_daily for select to authenticated
             using (organization_id = private.current_org_id())';
  end if;
end $$;
