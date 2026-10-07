"""Journeys de marketing: segmentos → recorridos de varios pasos por WhatsApp, correo y chat web (§21.1).

- `schema`: formato del grafo de pasos y su validación (JSON Schema + alcanzabilidad, sin ciclos, esperas ≥ 1 min,
  pesos A/B que suman 100).
- `engine`: inscripción, ejecución de pasos (idempotente), límites de frecuencia, horas de silencio, consentimiento,
  selección de canal, ramas, A/B, metas y salidas.
- `hooks`: estados de mensajes y respuestas del cliente → eventos del journey (desde app/ingest.py).
- `scheduler`: bucle que programa inscripciones vencidas, refresca segmentos, entradas por fecha y por eventos.
"""
