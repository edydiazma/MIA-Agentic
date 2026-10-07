"""Base de conocimiento con RAG (docs/data-model.md §21.2).

- text: limpieza, idioma, fragmentación por títulos/párrafos con solapamiento, datos personales.
- parsers: PDF, Word, Excel, CSV, texto/Markdown y HTML a texto.
- embeddings: Voyage AI / OpenAI (1024 dimensiones) y un proveedor determinista para pruebas; costo en ai_calls.
- ingest: fuentes (archivos, sitio web, catálogo, conversaciones resueltas, documentos anteriores, FAQ) →
  documentos → fragmentos con embedding; omite lo que no cambió (checksum).
- retrieve: búsqueda híbrida (public.knowledge_search), rerank opcional, bloque con citas para el agente y
  registro de consultas.
- gaps: preguntas sin respuesta agrupadas por similitud.
"""
