# Lista de verificación de lanzamiento

Para cada despliegue a producción. Si un paso falla, se detiene y se decide rollback (abajo).

## Antes

- [ ] CI verde: `ruff`, suite completa del backend (`pytest`, también con `-p randomly` — el orden no debe importar),
      lint/typecheck del frontend.
- [ ] Probado en **staging** con la misma versión y las mismas migraciones.
- [ ] Revisa las migraciones nuevas (`git diff --stat origin/main -- supabase/migrations`):
  - ¿Son **compatibles hacia atrás** (el código viejo sigue funcionando con el esquema nuevo)? Agregar columnas
    nulas / tablas / índices `concurrently` sí; renombrar o borrar columnas, no — eso va en dos lanzamientos
    (1: dejar de usarla; 2: borrarla).
  - ¿Alguna reescribe una tabla grande (`messages`, `contact_changes`, `inbound_events`)? Programa una ventana.
- [ ] Respaldo reciente: PITR activo o un respaldo de las últimas 24 h ([backups.md](backups.md)).
- [ ] Aviso en el canal de operaciones: qué sale, quién despliega, hora.

## Orden de despliegue

1. **Migraciones primero** (desde la máquina de operaciones):
   ```bash
   npx supabase db push --linked          # en orden por nombre de archivo
   npx supabase db advisors --linked --type all   # sin errores de seguridad nuevos
   ```
2. **Preflight como compuerta** (con la imagen nueva, contra producción; solo lectura):
   ```bash
   cd deploy && docker compose build backend
   docker compose run --rm --no-deps backend python -m app.preflight --all-orgs --trigger deploy
   echo $?   # 0 = sigue · 1 = avisos: lee cada uno y decide · 2 = FALLA: no despliegues
   ```
3. **Aplicación:** `./update.sh` (worker → réplicas nuevas de la API con chequeo de salud → retira las viejas →
   frontend → Caddy). Si `update.sh` sale con error, las réplicas viejas siguen atendiendo.

## Humo

En los 10 minutos siguientes (anota hora y resultado):

- [ ] `curl -fsS https://<dominio>/health/ready`
- [ ] Login en el panel; la bandeja carga; abrir una conversación con medios (imagen se ve).
- [ ] Mensaje real de prueba desde un teléfono del equipo al número de pruebas → aparece en la bandeja en < 5 s, el bot
      responde (si aplica) y la respuesta llega al teléfono (✓✓).
- [ ] Responder como asesor → llega al teléfono.
- [ ] Reporte en tiempo real y tablero cargan.
- [ ] `/api/ops/jobs`: sin trabajos muertos nuevos, cola no creciendo.
- [ ] Logs: `docker compose logs --since 10m backend worker | grep -c Traceback` → 0 (o explicados).
- [ ] Si el lanzamiento tocó voz / campañas / CRM / tracking: su prueba específica.

## Rollback

- **Código:** vuelve al commit anterior y despliega igual: `git checkout <sha-anterior> && SKIP_PULL=1 ./update.sh`.
  Es seguro **si** las migraciones del lanzamiento eran compatibles hacia atrás (ver «Antes»).
- **Migración:** no hay `down` automático. Escribe una migración nueva que revierta (con prueba en staging) — no edites
  ni borres migraciones ya aplicadas. Si hubo pérdida de datos, PITR al instante previo ([backups.md](backups.md)).
- Tras un rollback: aviso en el canal, issue con la causa, y el lanzamiento corregido pasa de nuevo por esta lista.
