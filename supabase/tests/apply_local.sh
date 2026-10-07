#!/usr/bin/env bash
# Aplica las migraciones + seed en un Postgres local temporal, dos veces:
#   1) Postgres "vainilla"  2) Postgres con stubs de los esquemas de Supabase (auth, realtime, storage).
# Uso: supabase/tests/apply_local.sh <directorio_de_trabajo> [puerto]
set -euo pipefail
WORK=${1:?directorio de trabajo}
PORT=${2:-55432}
PGB=${PGB:-/opt/homebrew/opt/postgresql@17/bin}
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export PGOPTIONS="-c search_path=public,extensions"
PSQL=("$PGB/psql" -h localhost -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q)

if ! "$PGB/pg_isready" -h localhost -p "$PORT" >/dev/null 2>&1; then
  rm -rf "$WORK" && mkdir -p "$WORK"
  "$PGB/initdb" -D "$WORK/data" -U postgres -A trust >/dev/null
  "$PGB/pg_ctl" -D "$WORK/data" -o "-p $PORT -k '' -c listen_addresses=localhost" -l "$WORK/log.txt" -w start >/dev/null
fi

apply() {
  local db=$1
  for f in "$HERE"/migrations/*.sql "$HERE"/seed.sql; do
    if ! out=$("${PSQL[@]}" -d "$db" -f "$f" 2>&1); then
      echo "FALLA en $db: $(basename "$f")"; echo "$out" | tail -8; return 1
    fi
  done
  echo "OK $db: migraciones aplicadas"
}

for db in vanilla supa; do "$PGB/dropdb" -h localhost -p "$PORT" -U postgres --if-exists "$db"; done
"$PGB/createdb" -h localhost -p "$PORT" -U postgres vanilla
apply vanilla

"$PGB/createdb" -h localhost -p "$PORT" -U postgres supa
"${PSQL[@]}" -d supa -f "$HERE/tests/supabase_stubs.sql"
apply supa
