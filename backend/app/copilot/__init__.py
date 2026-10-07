"""Copiloto de IA para asesores y asistente del supervisor (docs/data-model.md §19.3).

- llm.py: llamadas al Cortex (propósitos «copilot» / «assistant»), una sola puerta para poder simularlas en pruebas.
- context.py: contexto de la conversación (historial, cliente sin datos sensibles, memoria, catálogo, etapa…).
- guard.py: no inventar precios ni disponibilidad (validación de lo que propone la IA).
- core.py: sugerencias de respuesta + siguiente mejor acción (una sola llamada), borradores, reescritura,
  resúmenes de traspaso y de cierre, resultado de cada sugerencia (adopción y distancia de edición).
- hooks.py: disparadores (mensaje del cliente con antirrebote, transferencia, cierre) que nunca lanzan.
- assistant.py: chat del supervisor con herramientas sobre los reportes (sin SQL libre), respetando su alcance.
"""
