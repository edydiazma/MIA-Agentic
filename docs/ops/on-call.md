# Guardia — hoja rápida

**Primero:** ¿afecta a todos o a una empresa? ¿Entran mensajes? ¿Salen? Eso decide la severidad
([incident-response.md](incident-response.md)).

## Accesos que debes tener antes de tu turno

EC2 (SSM o SSH desde tu IP) · Supabase (proyecto prod) · Meta Business Manager (app + WABA) · AWS (S3 respaldos,
Secrets Manager, CloudWatch) · DNS · gestor de contraseñas · canal de operaciones.

## Comandos (en la EC2, `cd ~/wa-agent-platform/deploy`)

| Para | Comando |
|---|---|
| Estado de contenedores | `docker compose ps` |
| Errores recientes | `docker compose logs --since 15m backend worker \| grep -iE "error\|traceback" \| tail -50` |
| Salud | `curl -s localhost/health/ready` |
| Diagnóstico de integraciones | `docker compose exec backend python -m app.preflight --all-orgs` (0 ok · 1 avisos · 2 fallas) |
| Una empresa | `docker compose exec backend python -m app.preflight --org <id> --areas meta` |
| Métricas | `docker compose exec backend sh -c 'curl -s -H "Authorization: Bearer $METRICS_TOKEN" localhost:8000/metrics' \| grep -E "wa_jobs\|5xx"` |
| Reinicio escalonado | `SKIP_PULL=1 ./update.sh` |
| Reiniciar solo el worker | `docker compose restart worker` |
| Más workers | `docker compose up -d --scale worker=2` |
| CPU/memoria | `docker stats --no-stream` |

## SQL útil (Supabase → SQL editor, solo lectura salvo indicación)

```sql
-- conexiones por estado
select state, count(*) from pg_stat_activity group by 1;
-- consultas largas
select pid, now()-query_start dur, left(query,100) from pg_stat_activity where state<>'idle' order by 2 desc limit 10;
-- cola de trabajos
select queue, status, count(*), min(run_at) from public.jobs group by 1,2 order by 1,2;
-- latido de workers
select * from public.worker_heartbeats order by last_beat_at desc;
-- eventos de Meta recibidos en los últimos 10 min
select count(*) from public.inbound_events where created_at > now() - interval '10 minutes';
```

## Umbrales que deben preocuparte

| Señal | Normal | Actúa si |
|---|---|---|
| `wa_jobs_oldest_ready_seconds` | < 10 s | > 120 s sostenido |
| 5xx en `/webhooks/whatsapp` | 0 | cualquiera sostenido (Meta deshabilita el webhook si falla mucho) |
| Conexiones del pooler | < 70 % del límite | > 85 % |
| `wa_jobs_dead` | estable | sube |
| CPU EC2 / créditos t3 | < 70 % / > 0 | 90 % / créditos en 0 |
| Calidad de un número | VERDE | AMARILLO → avisa · ROJO → pausa campañas |

## Escalamiento

1. Guardia (tú) → 2. Responsable técnico de la plataforma → 3. Soporte de Supabase (plan Pro: ticket desde el panel)
/ Soporte de Meta (Business Support, «Direct Support» si está contratado).

## No hagas

- No reenvíes webhooks de Meta a mano (Meta reintenta; duplicarías mensajes).
- No borres filas de `jobs` ni `inbound_events` para «destrabar»: márcalas o reencola desde el panel.
- No cambies `DATABASE_URL` al pooler de transacciones (6543).
- No pegues tokens ni contraseñas en Slack/WhatsApp/tickets: usa el gestor de contraseñas.
- No hagas `docker compose down` en producción (tumba Caddy y todas las réplicas a la vez).
