# Retención de datos y privacidad (habeas data)

Marco: Ley 1581 de 2012 (Colombia) y equivalentes en otros países donde operen las empresas clientes. La plataforma
es **encargada** del tratamiento; cada empresa (cliente) es **responsable** y atiende las solicitudes de sus titulares.
Nuestro deber: darles las herramientas y cumplir sus instrucciones en ≤ 15 días hábiles (consultas: 10).

## Solicitudes de titulares (panel / API)

Permiso nuevo: **`contacts.privacy`** («Exportar y eliminar datos personales»). Los administradores lo tienen; asígnalo a
roles con criterio (normalmente solo el oficial de protección de datos). Ambos endpoints respetan el alcance de datos
del usuario (404 si el cliente no está en su alcance).

| Acción | Endpoint | Resultado |
|---|---|---|
| Acceso / portabilidad | `GET /api/contacts/{id}/export` | JSON con el contacto, identidades, campos, etiquetas, historial de cambios, golden record, vehículos, consentimientos, productos de interés, atribución, negocios, citas, seguimientos, llamadas, campañas recibidas, sesiones de chat web y todas sus conversaciones con mensajes. Excluye hashes técnicos (`token_hash`, `ip_hash`). |
| Supresión | `DELETE /api/contacts/{id}/erase` con cuerpo `{"confirm": "ELIMINAR"}` | Ver abajo. Irreversible. |

**Qué hace la supresión** (`app/privacy.py`): borra los archivos del cliente en Storage; vacía texto, transcripción,
medios y metadatos de sus mensajes y los resúmenes/vista previa/datos de anuncio de sus conversaciones; borra
sugerencias del copiloto e hilos de correo; borra identidades, campos, etiquetas, llaves, golden record, vehículos,
consentimientos, productos de interés, candidatos de fusión; quita identificadores de clic (gclid, fbclid, ctwa_clid)
y URLs de atribución; anonimiza negocios/seguimientos/citas (`[eliminado]`), borra grabaciones, transcripciones y
turnos de llamadas; el contacto queda como `[eliminado]` sin teléfono, BSUID, correo, notas ni memoria, con opt-out.
Se registra un `contact_changes` con `field_key = 'erased'` (quién y cuándo, sin datos personales).

**Qué se conserva a propósito:** las filas de conversaciones, mensajes, negocios y llamadas (vacías de contenido) para que
los reportes agregados y la facturación sigan cuadrando; montos de negocios; el registro de auditoría de la solicitud.

**Después de borrar:** si el cliente vuelve a escribir, entra como contacto nuevo (sin relación con el anterior). Los
datos ya enviados a sistemas externos (CRM, Google Ads, Meta CAPI) **no** se borran desde aquí: la empresa debe
pedirlo en cada sistema; el JSON de exportación sirve para saber qué se envió.

## Retención automática

| Dato | Retención | Cómo |
|---|---|---|
| `inbound_events` (payloads crudos de Meta) | 1 mes | cron `retention` → `drop_old_partitions` |
| `ai_calls`, `flow_run_steps`, `webhook_deliveries` | 6 meses | cron `retention` |
| `rate_limit_counters` | 2 h | cron `ops-cleanup` |
| `realtime_spill` | 10 min | cron `ops-cleanup` |
| Conversaciones, mensajes, contactos | Mientras la empresa sea cliente | — |
| Empresa que termina contrato | 90 días tras la baja; luego borrado | manual (operaciones), con acta |
| Respaldos de base (PITR / `pg_dump`) | 7 días / 90 días | ciclo de vida de S3 |
| Respaldos de Storage | 90 días (versiones previas) | ciclo de vida de S3 |

**Respaldos y supresión:** una supresión no reescribe respaldos. Los datos borrados desaparecen de los respaldos al
expirar estos (≤ 90 días). Si se restaura un respaldo anterior a una supresión, hay que **repetir las supresiones**
posteriores a la fecha del respaldo: `select contact_id, created_at from contact_changes where field_key = 'erased'
and created_at > '<fecha del respaldo>'` y volver a ejecutar `erase` para cada uno.

## Otros controles

- Los secretos de integraciones viven en Vault, nunca en tablas ni logs.
- Consentimientos (`contact_consents`: habeas_data, marketing, call_recording…) se registran con fecha y fuente; las
  campañas respetan el opt-out.
- Grabación de llamadas solo con consentimiento `call_recording` cuando la empresa lo exige.
- Acceso de operaciones a datos de clientes: solo para soporte, con ticket y queda en auditoría.
