# Pruebas de carga

Nunca contra producción. Corre contra **staging** (mismo tamaño de EC2 y mismo plan de Supabase que producción) con la
Graph API **falsa**, para que no salga ni un mensaje real a Meta.

| Archivo | Qué hace |
|---|---|
| `k6/webhook_storm.js` | Tormenta de webhooks de WhatsApp firmados (X-Hub-Signature-256): texto, imágenes, estados, clientes solo con BSUID (5 %), referrals Click to WhatsApp (10 %). Rampa a `PEAK_RPS` sobre `PHONES` números. |
| `k6/panel_mix.js` | `AGENTS` asesores: bandeja, abrir conversación, responder, reporte en tiempo real. |
| `k6/public_endpoints.js` | `/t/collect`, `/w/session` + `/w/messages`, `/hooks/{slug}`, `/v1/contacts`. Un 429 cuenta como éxito (el límite funciona). |
| `webhook_storm.py` | Lo mismo que `webhook_storm.js` en Python (asyncio + httpx), si no tienes k6. |
| `fake_meta.py` | Graph API falsa (envíos, leído, medios, plantillas) con latencia y tasa de error configurables. |

## 1. Preparar staging

1. **Graph API falsa.** Levántala en una máquina a la que el backend llegue (otra EC2 o la misma):
   ```bash
   cd backend && FAKE_META_LATENCY_MS=120 FAKE_META_ERROR_RATE=0.005 \
     .venv/bin/uvicorn --app-dir ../loadtest fake_meta:app --host 0.0.0.0 --port 9900 --workers 2
   ```
   En el `.env` de staging pon `WA_GRAPH_BASE=http://<host>:9900` y reinicia (`./update.sh` con `SKIP_PULL=1`).
   Confírmalo con `curl http://<host>:9900/stats` después de unos envíos: el contador `messages` debe subir.
   ⚠️ La voz (`app/voice/meta.py`) y la API de Conversiones de Meta (`app/conversions.py`) aún tienen la URL de Meta fija:
   no hagas llamadas de voz y deja la conversión a Meta apagada en staging durante la prueba.
2. **Canal de staging:** el `PHONE_NUMBER_ID` y `WABA_ID` que pases deben ser de un canal existente en staging (si no, el
   webhook se acepta pero se descarta como «número desconocido» y no mides el procesamiento).
3. **IA:** el bot llama al modelo en cada mensaje. Para medir la plataforma (no al proveedor de IA) pon los agentes de IA del
   canal de staging en pausa, o usa un proveedor con límite de gasto. Si lo dejas encendido, vigila el costo.
4. **Asesor de carga** para `panel_mix.js`: un usuario sin segundo factor y con IP permitida desde la máquina de k6.
5. **Límites de uso:** las pruebas públicas salen de una sola IP; los límites por IP (`/t/collect`, `/w/*`, login) van a
   devolver 429 rápido. Eso es lo esperado: lo que mides es que el 429 sea barato y no tumbe nada.

## 2. Correr

```bash
brew install k6   # o https://k6.io/docs/get-started/installation/

# a) Webhooks: 10 → 200 msg/s en 5 min, 10 min sostenido, 5000 clientes distintos
k6 run -e BASE_URL=https://staging.tu-dominio.com -e WA_APP_SECRET="$WA_APP_SECRET" \
       -e PHONE_NUMBER_ID=... -e WABA_ID=... -e PEAK_RPS=200 -e PHONES=5000 loadtest/k6/webhook_storm.js

# b) Panel: 50 asesores, 10 min
k6 run -e BASE_URL=... -e AGENT_EMAIL=carga@empresa.com -e AGENT_PASSWORD="$PW" -e AGENTS=50 loadtest/k6/panel_mix.js

# c) Públicos (omite los que no configures)
k6 run -e BASE_URL=... -e SITE_KEY=... -e SITE_ORIGIN=https://sitio-permitido.com -e WIDGET_KEY=... \
       -e HOOK_SLUG=... -e HOOK_TOKEN=... -e API_KEY=... -e RPS=20 loadtest/k6/public_endpoints.js

# Sin k6:
cd backend && WA_APP_SECRET=... .venv/bin/python ../loadtest/webhook_storm.py --base https://staging... --rps 50 --seconds 300
```

Pasa los secretos por variable de entorno, nunca escritos en el comando (quedan en el historial).

**Umbrales** (k6 sale con código ≠ 0 si se rompen):

| Escenario | p95 | Errores |
|---|---|---|
| Webhook (`200` a Meta) | < 300 ms (p99 < 800 ms) | < 0,5 % |
| Panel: bandeja / mensajes | < 500 ms | < 1 % |
| Panel: responder (incluye Graph falsa) | < 1,5 s | < 1 % |
| Panel: reporte en tiempo real | < 2 s | < 1 % |
| Públicos | < 400 ms | checks > 99 % (429 cuenta como éxito) |

## 3. Qué mirar durante la prueba

- **`/metrics`** de cada réplica (`curl -H "Authorization: Bearer $METRICS_TOKEN" http://backend:8000/metrics` desde la red
  de Docker): `wa_http_request_duration_seconds` por ruta, `wa_http_requests_total{status="5xx"}`, `wa_rate_limited_total`,
  `wa_jobs_queue_depth` y `wa_jobs_oldest_ready_seconds` (la cola debe vaciarse; si la antigüedad crece sin parar, el
  worker no da abasto), `wa_jobs_dead`, `wa_realtime_spilled_total` y `wa_realtime_errors_total`, `wa_loop_restarts_total`.
- **Supabase → Database → Pooler:** conexiones de cliente frente al límite del plan. Cada réplica de API usa
  `DB_POOL_SIZE + DB_MAX_OVERFLOW + 1`; si ves `QueuePool limit … timed out` en los logs, el cuello es el pool, no Postgres.
- **Supabase → Reports:** CPU y disk IO de la base, consultas lentas (`pg_stat_statements`).
- **EC2:** `docker stats` (CPU/memoria por contenedor), `uptime` (carga). `t3` usa créditos de CPU: en pruebas largas
  mira *CPUCreditBalance* en CloudWatch; si llega a 0, la instancia se frena y los números dejan de ser representativos.
- **Panel → Operación → Colas** (`/api/ops/jobs`): profundidad y trabajos fallidos por cola.
- **Graph falsa:** `GET :9900/stats` para ver cuántos envíos hizo realmente el backend.

## 4. Cuellos de botella esperados (en orden)

1. **Conexiones del pooler** de Supabase: es lo primero que se agota al subir réplicas o el pool. Solución: menos
   `DB_MAX_OVERFLOW` por réplica o un plan con más clientes en el pooler; no subas el pool a ciegas.
2. **Worker / cola de trabajos:** el webhook responde rápido (guarda el evento y encola), así que la latencia del 200 se
   mantiene, pero la cola crece. Mira `wa_jobs_oldest_ready_seconds`; escala con un segundo worker.
3. **Llamadas a la IA** (si no las apagaste): latencia de segundos y límites por minuto del proveedor.
4. **CPU de la EC2** con `t3.small` por encima de ~100 msg/s sostenidos (JSON + firma + ORM). Pasa a `t3.medium`/`c7g`.
5. **Fan-out en tiempo real:** con muchos asesores conectados, cada mensaje se publica por `NOTIFY`; los eventos grandes
   van a `realtime_spill` (sube `wa_realtime_spilled_total`).
6. **Reportes** del panel sobre tablas grandes: usa `DATABASE_URL_REPORTS` (réplica de lectura) si existe.

## 5. Después

Anota en el PR de go-live: PEAK alcanzado sin romper umbrales, réplicas y tamaño de pool usados, conexiones máximas del
pooler y el cuello que apareció primero. Borra los datos de carga de staging (contactos `Cliente N` con teléfonos
`573010…`) o restaura staging desde un respaldo.
