-- =============================================================================
-- 24b · Fuente del cliente: un regreso por otro anuncio dentro de la MISMA conversación actualiza la fila de
-- atribución (último toque) e incrementa touches; desde ahí esa fila ya no representa el primer toque. El primer
-- toque del cliente solo sigue a su fila mientras touches = 1 (p. ej. el enriquecimiento de nombres de campaña).
-- docs/data-model.md §16
-- =============================================================================
create or replace function private.trg_attributions_contact_source() returns trigger
language plpgsql set search_path = public, pg_temp as $$
declare
  v_label text := private.attribution_label(new);
  v_campaign text := coalesce(new.platform_campaign_name, new.utm_campaign);
  v_at timestamptz := coalesce(new.last_touch_at, new.created_at);
  v_first_touch boolean := coalesce(new.touches, 1) <= 1;
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
    and (k.first_attribution_id is null or k.first_source_channel = 'direct'
         or (k.first_attribution_id = new.id and v_first_touch));
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
