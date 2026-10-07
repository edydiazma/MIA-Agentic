# Operación

| Documento | Para |
|---|---|
| [release-checklist.md](release-checklist.md) | Cada despliegue: migraciones → `python -m app.preflight` (compuerta por código de salida) → `update.sh` → humo → rollback |
| [incident-response.md](incident-response.md) | Webhooks de Meta, token vencido, calidad ROJA, pooler agotado, cola atrasada, voz caída |
| [on-call.md](on-call.md) | Hoja rápida de guardia: comandos, SQL, umbrales, escalamiento |
| [backups.md](backups.md) | PITR, `pg_dump` semanal, Storage (`scripts/storage_sync.sh`), Vault, custodia del `.env` |
| [dr-drill.md](dr-drill.md) | Simulacro trimestral de DR (proyecto Supabase + EC2 nuevos), RPO/RTO |
| [privacy.md](privacy.md) | Habeas data (exportar / eliminar), retención automática, respaldos y supresión |
| [../../loadtest/README.md](../../loadtest/README.md) | Pruebas de carga contra staging |
