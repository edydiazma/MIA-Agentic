# Respaldos y restauración

Qué hay que poder recuperar y dónde vive:

| Dato | Dónde | Respaldo | RPO |
|---|---|---|---|
| Base de datos (todo el negocio) | Supabase Postgres | **PITR** (add-on, plan Pro+) + respaldo diario automático | ~2 min con PITR; 24 h sin él |
| Archivos (medios de conversaciones, recursos) | Supabase Storage, buckets `conversation-media`, `resources` | `scripts/storage_sync.sh` → S3 de otra cuenta, diario | 24 h |
| Secretos de integraciones (tokens de WhatsApp, LLM, CRM) | Supabase Vault (`vault.secrets`) | Van dentro del respaldo de la base, cifrados con la llave del proyecto | igual que la base |
| Configuración del servidor | `deploy/.env` en la EC2 | Custodia cifrada (abajo) | en cada cambio |
| Código | Git | remoto | — |

## Base de datos

- **Activa PITR** en producción (Project Settings → Add-ons → Point in time recovery, retención ≥ 7 días). Sin PITR,
  Supabase guarda un respaldo diario y el RPO es de hasta 24 h: no es aceptable con conversaciones en vivo.
- **Copia lógica semanal fuera de Supabase** (protege contra perder la cuenta, no solo la base):
  ```bash
  # desde la máquina de operaciones, con la URL directa (no el pooler de transacciones)
  pg_dump "$DATABASE_URL_DIRECT" -Fc --no-owner --exclude-schema=vault -f wa-$(date +%F).dump
  aws s3 cp wa-$(date +%F).dump s3://$BKP_BUCKET/pg/ --sse aws:kms
  ```
  `vault` se excluye porque sus valores solo se descifran con la llave del proyecto original; ver «Vault» abajo.
- Las tablas particionadas por mes (`messages`, `inbound_events`, `ai_calls`, …) se restauran con sus particiones; tras
  restaurar en un proyecto nuevo, corre `select public.ensure_monthly_partitions(...)` (lo hace el cron `partitions-ahead`
  el día 1; si restauras otro día, ejecútalo a mano: ver la migración que lo define).

### Restaurar (PITR)

1. Decide el instante: el último bueno **antes** del incidente (mira `contact_changes`, `audit_log` o los logs).
2. Supabase → Database → Backups → Point in time → elige fecha y hora → Restore. **Reemplaza la base del proyecto**
   (hay corte): avisa antes y para el tráfico entrante si puedes (`docker compose stop backend worker`; Meta reintenta
   los webhooks hasta 7 días, no se pierden).
3. Arranca el worker y la API, corre `python -m app.preflight --all-orgs` (debe salir 0 o 1) y las pruebas de humo
   de [release-checklist.md](release-checklist.md#humo).
4. Si solo hace falta **una tabla o unos registros**, NO restaures encima: restaura PITR en un **proyecto nuevo**
   (o el `pg_dump` semanal en una base local) y copia los registros con `\copy`.

## Storage

`docs/ops/scripts/storage_sync.sh` hace `rclone copy` (no `sync`) de ambos buckets a un bucket S3 **con versionado**, en
otra cuenta de AWS. Prográmalo diario (cron/EventBridge en una máquina de operaciones). Verifica cada mes que el número
de objetos coincide (`rclone size supa:conversation-media` vs `rclone size bkp:…`).

Restaurar: `RESTORE=1 TARGET=supa-dr: BKP_BUCKET=… ./storage_sync.sh` hacia el proyecto destino (crea antes los buckets
como **privados** con el mismo nombre). Las rutas guardadas en la base (`<bucket>/<org>/<…>`) no cambian.

## Vault

Los secretos de Vault se cifran con una llave que **no sale del proyecto Supabase**. Política:

- Un respaldo/PITR **del mismo proyecto** restaura los secretos tal cual.
- En un **proyecto nuevo** (DR) los secretos no se pueden descifrar: hay que volver a cargarlos. Por eso:
  - Mantén el inventario (sin valores): `select name, created_at, updated_at from vault.secrets order by name;` →
    se exporta mensualmente al gestor de contraseñas de operaciones.
  - Los valores de origen (tokens de sistema de Meta, llaves de LLM, CRM) viven en sus consolas y en el gestor de
    contraseñas del equipo (1Password / AWS Secrets Manager), nunca en Git, Slack ni chats.
  - Tras un DR, las empresas vuelven a conectar sus integraciones desde el panel, o operaciones recarga los tokens del
    servidor; `python -m app.preflight --all-orgs` lista cuáles faltan.

## `.env` (custodia)

- El `.env` de producción se guarda cifrado en AWS Secrets Manager (o 1Password, bóveda «WA Agent – Prod») tras **cada**
  cambio, con nota de qué cambió. Dos personas con acceso.
- Nunca en Git, en el AMI ni en respaldos sin cifrar de la EC2.
- Para recuperarlo: `aws secretsmanager get-secret-value --secret-id wa-agent/prod/env --query SecretString --output text > deploy/.env && chmod 600 deploy/.env`.

## Calendario

| Cuándo | Qué |
|---|---|
| Diario | PITR (automático), `storage_sync.sh` |
| Semanal | `pg_dump` a S3 |
| Mensual | Restaurar el `pg_dump` en una base local y contar filas; `rclone size`; exportar inventario de Vault |
| Trimestral | [Simulacro de DR](dr-drill.md) completo |
