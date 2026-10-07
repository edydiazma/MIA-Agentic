#!/usr/bin/env bash
# Actualización escalonada sin cortar el servicio:
#   1. trae el código y construye las imágenes nuevas;
#   2. reemplaza el worker (las tareas de fondo pasan al nuevo cuando toma los locks);
#   3. levanta réplicas NUEVAS de la API junto a las viejas, espera a que estén sanas (/health/ready) y recién
#      entonces retira las viejas (Caddy deja de enviarles tráfico al desaparecer del DNS de Docker);
#   4. actualiza el frontend y Caddy.
# Las migraciones de base de datos NO se aplican aquí: se aplican antes con `npx supabase db push`.
# Uso: ./update.sh            (git pull + build)
#      SKIP_PULL=1 ./update.sh (el código ya está en la versión deseada, p. ej. desde el workflow de deploy)
set -euo pipefail
cd "$(dirname "$0")"

REPLICAS=$(grep -E '^BACKEND_REPLICAS=' .env 2>/dev/null | cut -d= -f2 || true)
REPLICAS=${REPLICAS:-2}
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2)

wait_healthy() {  # ids de contenedor; espera hasta 3 min a que el HEALTHCHECK (/health/ready) diga healthy
  local deadline=$((SECONDS + 180)) id status
  for id in "$@"; do
    while true; do
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
      [ "$status" = "healthy" ] && break
      if [ "$status" = "unhealthy" ] || [ $SECONDS -gt $deadline ]; then
        echo "✗ El contenedor $id no quedó sano ($status). Últimos logs:" >&2
        docker logs --tail 50 "$id" >&2 || true
        return 1
      fi
      sleep 3
    done
  done
}

[ -n "${SKIP_PULL:-}" ] || git pull --ff-only
docker compose build --pull

echo "→ worker"
docker compose up -d --no-deps worker
wait_healthy $(docker compose ps -q worker)

# Nodo de voz (si está en uso): reemplazarlo corta las llamadas que estén atendiendo los agentes de voz IA.
# Por eso solo se actualiza si no hay sesiones activas o con FORCE_VOICE=1.
if [ -n "$(docker compose --profile voice ps -q voice 2>/dev/null)" ]; then
  load=$(docker compose --profile voice exec -T voice python -c "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready')).get('voice_sessions', 0))" 2>/dev/null || echo 0)
  if [ "${load:-0}" = "0" ] || [ -n "${FORCE_VOICE:-}" ]; then
    echo "→ voz"
    docker compose --profile voice up -d --no-deps voice
    wait_healthy $(docker compose --profile voice ps -q voice)
  else
    echo "⚠ El nodo de voz tiene $load llamadas activas: no se actualizó (FORCE_VOICE=1 para forzar)" >&2
  fi
fi

echo "→ API: $REPLICAS réplicas nuevas junto a las actuales"
OLD=$(docker compose ps -q backend)
docker compose up -d --no-deps --no-recreate --scale backend=$((REPLICAS + $(echo "$OLD" | grep -c . || true))) backend
NEW=$(comm -13 <(echo "$OLD" | sort) <(docker compose ps -q backend | sort))
if ! wait_healthy $NEW; then
  echo "Se retiran las réplicas nuevas; las anteriores siguen atendiendo." >&2
  [ -z "$NEW" ] || docker rm -f $NEW >/dev/null
  exit 1
fi
if [ -n "$OLD" ]; then
  echo "→ retirando réplicas anteriores"
  docker stop --time 20 $OLD >/dev/null   # SIGTERM: cierra los WebSockets; el panel se reconecta a otra réplica
  docker rm $OLD >/dev/null
fi
docker compose up -d --no-deps --no-recreate --scale backend="$REPLICAS" backend

echo "→ frontend y caddy"
docker compose up -d --no-deps frontend caddy
docker image prune -f >/dev/null
docker compose ps
curl -fsS "https://${DOMAIN}/health/ready" >/dev/null && echo "✓ backend listo (https://${DOMAIN}/health/ready)"
