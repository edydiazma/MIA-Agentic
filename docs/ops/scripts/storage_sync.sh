#!/usr/bin/env bash
# Copia de seguridad de Supabase Storage → S3 (u otro destino de rclone). Incremental: solo sube lo nuevo o cambiado.
# Los respaldos de Postgres (PITR) NO incluyen los archivos de Storage: sin esto, una restauración deja las
# conversaciones sin sus medios.
#
# Requisitos: rclone ≥ 1.60 con dos remotos configurados (rclone config):
#   supa:  type=s3, provider=Other, endpoint=https://<ref>.supabase.co/storage/v1/s3, region=<región del proyecto>,
#          access_key_id / secret_access_key = llaves S3 de Supabase (Project Settings → Storage → S3 access keys)
#   bkp:   type=s3, provider=AWS, bucket con versionado + regla de ciclo de vida (ej. borrar versiones previas a 90 días),
#          en otra cuenta/región que producción
# Uso:   BKP_BUCKET=wa-agent-backups ./storage_sync.sh          (cron diario en una máquina de operaciones, no en la EC2 de la app)
#        DRY_RUN=1 ./storage_sync.sh                            (muestra qué copiaría)
#        RESTORE=1 TARGET=supa-dr: ./storage_sync.sh            (dirección inversa: del respaldo a un proyecto nuevo)
set -euo pipefail

BUCKETS=(conversation-media resources)   # app/storage.py: MEDIA_BUCKET, RESOURCES_BUCKET
SRC=${SOURCE:-supa:}
DST_BASE=${TARGET:-bkp:${BKP_BUCKET:?Falta BKP_BUCKET}/storage}
FLAGS=(--fast-list --transfers 16 --checkers 32 --s3-no-check-bucket --stats-one-line --stats 60s)
[ -n "${DRY_RUN:-}" ] && FLAGS+=(--dry-run)

for b in "${BUCKETS[@]}"; do
  if [ -n "${RESTORE:-}" ]; then
    # Restauración: del respaldo (BKP_BUCKET) al proyecto destino (TARGET=supa-dr:)
    echo "→ restaurando $b"
    rclone copy "bkp:${BKP_BUCKET:?Falta BKP_BUCKET}/storage/$b" "${TARGET:?Falta TARGET}$b" "${FLAGS[@]}"
  else
    echo "→ respaldando $b"
    # copy (no sync): un borrado accidental en producción NO borra el respaldo. El borrado por habeas data se
    # propaga con la regla de ciclo de vida del bucket de respaldo (ver docs/ops/privacy.md).
    rclone copy "$SRC$b" "$DST_BASE/$b" "${FLAGS[@]}"
  fi
done
echo "✔ listo $(date -u +%FT%TZ)"
