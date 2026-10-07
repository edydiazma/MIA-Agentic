-- =============================================================================
-- 22b · Productos por interacción: al borrar uno, la ficha descuenta el conteo y recalcula el último producto
-- (el trigger de 22 solo sumaba al insertar). docs/data-model.md §14
-- =============================================================================
create or replace function private.trg_interaction_products_contact_del() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  update public.contacts k set
    products_count = greatest(k.products_count - 1, 0),
    last_product_name = (select p.name from public.interaction_products p where p.contact_id = k.id
                         order by p.created_at desc, p.id desc limit 1)
  where k.id = old.contact_id;
  return null;
end $$;
revoke all on function private.trg_interaction_products_contact_del() from public;
create trigger trg_interaction_products_contact_del after delete on public.interaction_products
  for each row execute function private.trg_interaction_products_contact_del();
