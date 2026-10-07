# Respuesta a incidentes

**Proceso:** 1) reconoce la alerta en el canal de guardia · 2) evalúa severidad · 3) mitiga primero, causa raíz después ·
4) comunica · 5) postmortem sin culpables en ≤ 3 días hábiles para SEV1/SEV2.

| Severidad | Ejemplo | Respuesta |
|---|---|---|
| SEV1 | No entran ni salen mensajes de WhatsApp para todos; panel caído | Inmediata, 24/7; aviso a clientes en 30 min |
| SEV2 | Una empresa sin mensajes; cola atrasada > 15 min; voz caída | < 30 min en horario, < 2 h fuera |
| SEV3 | Integración secundaria fallando (CRM, conversiones) | Siguiente día hábil |

Primeros comandos para cualquier incidente (en la EC2, carpeta `deploy/`):
```bash
docker compose ps                                  # ¿algo reiniciando / unhealthy?
docker compose logs --since 15m backend worker | grep -iE "error|traceback" | tail -50
curl -s localhost/health/ready                     # desde la EC2
cd ../backend && docker compose exec backend python -m app.preflight --all-orgs   # 0 ok · 1 avisos · 2 fallas
```

---

## Webhooks de Meta fallando

**Síntomas:** no entran mensajes; Meta → App → Webhooks muestra errores; alerta de Meta por correo «webhook failing».

1. ¿Llega tráfico? `docker compose logs caddy --since 10m | grep /webhooks/whatsapp | tail`. Si no llega nada: DNS,
   certificado (Caddy), Security Group, o la suscripción del WABA a la app (`python -m app.preflight --areas meta`).
2. ¿Llega y responde 403/401? Firma inválida → `WA_APP_SECRET` no coincide con la app de Meta (¿rotaron el secreto?).
3. ¿Responde 5xx o lento (> 10 s)? La base no responde → ver «Pooler agotado».
4. ¿Responde 200 pero no aparecen mensajes? Se guardan en `inbound_events` y los procesa el worker → ver «Cola atrasada».
   Revisa también eventos descartados por número desconocido (`phone_number_id` sin canal).
5. Meta reintenta durante 7 días con backoff: al corregir, los mensajes llegan solos (puede tardar minutos).
   **No** reenvíes manualmente.

## Token de WhatsApp vencido o revocado

**Síntomas:** envíos fallan con error 190 / «Error validating access token»; alerta en el panel de la empresa.

1. Identifica si es el token del servidor (`WA_ACCESS_TOKEN`) o el de una empresa (Vault). El log trae el canal.
2. Usa siempre **token de usuario del sistema** (System User, sin vencimiento) con permisos `whatsapp_business_messaging`
   y `whatsapp_business_management`. Genéralo en Business Manager → Usuarios del sistema.
3. Empresa: Configuración → Canales → reconectar (Embedded Signup) o pegar el token nuevo; se guarda en Vault.
   Servidor: actualiza `.env`, custodia (ver [backups.md](backups.md#env-custodia)) y `SKIP_PULL=1 ./update.sh`.
4. Verifica con `python -m app.preflight --areas meta --org <id>`. Los mensajes del bot que fallaron no se reintentan
   solos si pasaron la ventana de 24 h: avisa a la empresa.

## Calidad en ROJO / número limitado

**Síntomas:** alerta de calidad del número, `quality_rating=RED`, límite de mensajería bajado, plantillas pausadas.

1. Pausa de inmediato las **campañas** de esa empresa/número (panel → Campañas → pausar). La plataforma pausa
   plantillas que Meta marca, pero las campañas en curso deben detenerse a mano.
2. Revisa qué plantilla/campaña disparó los bloqueos (reportes de campañas: tasa de bloqueo, opt-outs).
3. Comunica a la empresa: no reanudar hasta que vuelva a AMARILLO/VERDE (Meta lo reevalúa en ~7 días).
4. Las conversaciones iniciadas por el cliente siguen funcionando: el bot y los asesores pueden seguir respondiendo.

## Pooler de Supabase agotado

**Síntomas:** `QueuePool limit … reached`, `remaining connection slots are reserved`, `too many clients`; 5xx/latencia
alta en todo; `/health/ready` falla.

1. Supabase → Database → Pooler: clientes conectados vs límite. ¿Quién los tiene?
   `select application_name, usename, state, count(*) from pg_stat_activity group by 1,2,3 order by 4 desc;`
2. Consultas colgadas o bloqueadas: `select pid, now()-query_start, state, left(query,120) from pg_stat_activity where state <> 'idle' order by 2 desc;`
   Termina las que lleven > 5 min y no sean migraciones: `select pg_terminate_backend(<pid>);`
3. Mitiga bajando réplicas o `DB_MAX_OVERFLOW` (cada réplica de API = pool + overflow + 1) y reinicia escalonado.
   Recuerda: **Session pooler (5432)** es obligatorio; el de transacciones (6543) rompe LISTEN y los locks.
4. Causa típica: pico de tráfico + reportes pesados en la base principal → usar `DATABASE_URL_REPORTS`; o un cliente
   externo (BI, script) conectado con el usuario de la app.

## Cola de trabajos atrasada

**Síntomas:** `wa_jobs_oldest_ready_seconds` alto, `wa_jobs_queue_depth` creciendo; mensajes entran pero el bot tarda.
Panel: Operación → Colas (`/api/ops/jobs`).

1. ¿Hay worker vivo? `docker compose ps worker`, `docker compose logs worker --since 10m`.
   Tabla `worker_heartbeats`: el último latido debe tener < 1 min.
2. ¿Qué cola? Si es una sola (ej. `media` o `crm`), suele ser una integración externa lenta o caída: los trabajos
   reintentan con backoff; revisa `last_error` en `/api/ops/jobs`.
3. ¿Falla todo? Base (ver pooler) o el proveedor de IA (límites por minuto, caída): revisa el estado del proveedor.
4. Escala: `docker compose up -d --scale worker=2` (el segundo toma trabajos y es respaldo caliente).
5. Trabajos muertos (`wa_jobs_dead`): una vez corregida la causa, reencólalos desde el panel; no los borres sin revisar.

## Nodo de voz caído

**Síntomas:** llamadas no conectan o se cortan; alerta de heartbeat de `voice`; Meta rechaza `connect`.

1. `docker compose ps voice`, `docker compose logs voice --since 15m`. Reinicia: `docker compose restart voice`.
2. Puertos UDP de medios abiertos en el Security Group (ver `deploy/README.md`) y la IP pública correcta en el `.env`.
3. Mientras está caído: el agente de voz no contesta; las llamadas quedan como perdidas y el fallback crea la
   conversación de WhatsApp para devolver la llamada. Avisa a las empresas con voz activa.
4. Si es el proveedor de STT/TTS/LLM, el log lo indica: cambia de proveedor en la configuración del agente de voz.

---

## Comunicación

Plantilla para clientes (correo / WhatsApp de soporte):
> Estamos presentando [síntoma] desde las [hora]. Los mensajes de sus clientes [no se pierden / se procesarán al
> restablecer]. Próxima actualización: [hora]. — Equipo de operaciones

Postmortem: cronología, impacto (empresas, mensajes, minutos), causa raíz, qué funcionó, acciones con responsable.
