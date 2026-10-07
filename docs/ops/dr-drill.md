# Simulacro de recuperación ante desastres (DR)

Objetivo: demostrar, cada trimestre, que podemos levantar la plataforma completa en un **proyecto de Supabase nuevo** y
una **EC2 nueva** a partir de los respaldos, y medir cuánto se tarda y cuánto se pierde.

**Objetivos:** RPO ≤ 15 min (base, con PITR) / ≤ 24 h (archivos) · RTO ≤ 4 h.

El simulacro usa un dominio aparte (`dr.tu-dominio.com`) y **no** cambia el webhook de Meta de producción.

## Antes

- [ ] Responsable y cronometrista asignados; anota la hora de inicio (T0).
- [ ] Acceso a: Supabase (org), AWS (EC2, S3 de respaldos, Secrets Manager), DNS, gestor de contraseñas.
- [ ] Último `pg_dump` semanal y respaldo de Storage identificados (anota sus fechas: definen el RPO real).

## Pasos

1. **Proyecto Supabase nuevo** (misma región). Anota `ref`. Habilita las extensiones que usan las migraciones
   (`pg_cron`, `pgcrypto`, `vector`, `supabase_vault` …; `npx supabase db push` falla y las nombra si falta alguna).
2. **Esquema:** `npx supabase link --project-ref <nuevo> && npx supabase db push` (aplica `supabase/migrations/*` en orden).
3. **Datos:**
   - Opción A (PITR, solo si el proyecto original sigue vivo): restaurar PITR en el proyecto nuevo desde el panel.
   - Opción B (desastre real, cuenta perdida):
     `pg_restore --data-only --disable-triggers --no-owner -d "$NEW_DB_URL_DIRECT" wa-AAAA-MM-DD.dump`
   Luego `select public.ensure_monthly_partitions(t::regclass, 3) …` (ver migración de `partitions-ahead`) y
   `select setval(...)` si alguna secuencia quedó atrás (`python -m app.preflight` lo detecta en la verificación de base).
4. **Storage:** crea los buckets privados `conversation-media` y `resources`; `RESTORE=1 TARGET=supa-dr: ./scripts/storage_sync.sh`.
5. **EC2:** instancia nueva según `deploy/README.md` §1 (Docker, swap). `git clone`, recupera el `.env` de Secrets
   Manager y cambia: `DOMAIN`, `DATABASE_URL` (Session pooler del proyecto nuevo), `SUPABASE_URL`, `SUPABASE_SECRET_KEY`,
   y `WA_GRAPH_BASE` → la Graph falsa si es solo simulacro (para que nada salga a clientes reales).
6. **Arranque:** `docker compose up -d` → `curl https://dr.tu-dominio.com/health/ready`.
7. **Secretos de Vault:** en un proyecto nuevo no se descifran (ver [backups.md](backups.md#vault)). En el simulacro,
   recarga solo los de **una** empresa de prueba y confirma que `python -m app.preflight --org <id>` pasa.
8. **Verificación** ([release-checklist.md](release-checklist.md#humo)): login, bandeja con conversaciones históricas,
   un medio antiguo se abre, envío de prueba (contra la Graph falsa), webhook firmado de prueba procesado, reportes.
9. Anota T1 (servicio usable). **RTO = T1 − T0.** **RPO = hora del incidente simulado − marca de tiempo del último
   mensaje restaurado** (`select max(created_at) from messages`).

## En un desastre real, además

- Cambia el DNS de producción a la nueva Elastic IP (TTL bajo: déjalo en 300 s de forma permanente).
- Meta: si el dominio no cambia, no hay que tocar el webhook; Meta reintenta los eventos pendientes hasta 7 días.
- Stripe / CRM / Google Ads: los webhooks apuntan al dominio, siguen funcionando.
- Comunica a las empresas qué ventana de datos se perdió (RPO) y que deben reconectar integraciones si Vault no se pudo recuperar.

## Después

- Destruye el proyecto y la EC2 del simulacro (contienen datos personales reales).
- Registro en `docs/ops/dr-log.md`: fecha, RTO, RPO, qué falló, acciones. Si RTO > 4 h o RPO > objetivo, abre tareas.
