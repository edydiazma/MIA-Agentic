-- Organización inicial (instalación de una sola empresa). Tipificaciones por defecto.
insert into public.organizations (id, name, slug, timezone)
values (1, 'Mi empresa', 'default', 'America/Bogota')
on conflict (id) do nothing;
select setval(pg_get_serial_sequence('public.organizations', 'id'), greatest((select max(id) from public.organizations), 1));

insert into public.typifications (organization_id, name, is_success, position) values
  (1, 'Venta', true, 1),
  (1, 'Consulta resuelta', false, 2),
  (1, 'Cotización enviada', false, 3),
  (1, 'Reclamo', false, 4),
  (1, 'Sin respuesta', false, 5),
  (1, 'Spam', false, 6),
  (1, 'Inactividad', false, 7)
on conflict (organization_id, name) do nothing;

-- Instalación propia: la empresa inicial queda en el plan Enterprise (en instalaciones nuevas el seed corre
-- después de la migración 14, que solo asigna plan a las empresas que ya existían).
update public.organizations set plan_id = (select id from public.plans where key = 'enterprise')
where id = 1 and plan_id is null;
