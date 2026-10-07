#!/usr/bin/env bash
# Actualiza la instancia: trae el código, reconstruye y reinicia sin tocar los certificados.
# Las migraciones de base de datos NO se aplican aquí: se aplican antes con `npx supabase db push`.
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose build --pull
docker compose up -d
docker image prune -f
docker compose ps
curl -fsS "https://$(grep ^DOMAIN= .env | cut -d= -f2)/health" && echo " ← backend OK"
