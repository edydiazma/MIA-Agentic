/** Base de conocimiento con RAG. Espejo de backend/app/routers/knowledge_base.py. */

export type KSourceType = "upload" | "website" | "catalog" | "conversations" | "legacy_docs" | "faq" | "api";
export const SOURCE_LABEL: Record<KSourceType, string> = {
  upload: "Archivos",
  website: "Sitio web",
  catalog: "Catálogo de productos",
  conversations: "Conversaciones resueltas",
  legacy_docs: "Documentos anteriores",
  faq: "Preguntas frecuentes",
  api: "API",
};
export const SOURCE_HINT: Record<KSourceType, string> = {
  upload: "PDF, Word (.docx), Excel (.xlsx), CSV, TXT, Markdown o HTML (máx. 25 MB por archivo).",
  website: "Rastrea el mismo dominio respetando robots.txt y el sitemap.",
  catalog: "Un documento por producto (precio, stock, descripción). Se actualiza solo.",
  conversations: "Conversaciones cerradas con tipificación de éxito y buen puntaje de calidad, sin datos personales.",
  legacy_docs: "La base de conocimiento anterior, sincronizada automáticamente.",
  faq: "Pares pregunta / respuesta. «Responder» un vacío agrega aquí.",
  api: "Documentos enviados por POST /api/knowledge-base/sources/{id}/documents.",
};

export type KSource = {
  id: number;
  type: KSourceType;
  name: string;
  config: Record<string, unknown>;
  status: "idle" | "syncing" | "ready" | "error";
  refresh_hours: number | null;
  documents_count: number;
  chunks_count: number;
  last_synced_at: string | null;
  last_error: string | null;
  ai_agent_ids: number[];
  is_active: boolean;
  created_at: string;
};

export type KDocStatus = "pending" | "processing" | "ready" | "failed" | "excluded";
export const DOC_STATUS: Record<KDocStatus, [string, "ok" | "bad" | "warn" | "neutral" | "info"]> = {
  pending: ["En cola", "info"],
  processing: ["Procesando", "info"],
  ready: ["Listo", "ok"],
  failed: ["Error", "bad"],
  excluded: ["Excluido", "neutral"],
};

export type KDocument = {
  id: number;
  source_id: number;
  title: string;
  uri: string | null;
  mime: string | null;
  language: string | null;
  status: KDocStatus;
  chunks_count: number;
  tokens: number | null;
  valid_until: string | null;
  error: string | null;
  metadata: Record<string, unknown>;
  updated_at: string;
};

export type KChunk = {
  id: number;
  ordinal: number;
  heading: string | null;
  content: string;
  tokens: number | null;
  embedded: boolean;
  embedding_model: string | null;
};

export type KPassage = {
  label: string;
  document_id: number;
  chunk_id: number;
  title: string;
  uri: string | null;
  heading: string | null;
  score: number;
  source: KSourceType | "";
  content: string;
};

export type KSearchResult = {
  query: string;
  latency_ms: number;
  top_score: number | null;
  semantic: boolean;
  passages: KPassage[];
  prompt: string;
};

export type KGap = {
  id: number;
  topic: string;
  examples: string[];
  occurrences: number;
  first_seen_at: string;
  last_seen_at: string;
  status: "open" | "answered" | "ignored";
  resolved_document_id: number | null;
};

export type KStats = {
  days: number;
  queries: number;
  answered: number;
  unanswered: number;
  answer_rate: number | null;
  avg_latency_ms: number | null;
  documents: number;
  chunks: number;
  failed_documents: number;
  open_gaps: number;
  embedding_cost_usd: number;
  embedding_tokens: number;
};

export type KSettings = {
  enabled: boolean;
  embedding_connection_id: number | null;
  rerank_connection_id: number | null;
  top_k: number;
  max_context_chars: number;
  min_score: number;
  conversations_min_qa_score: number;
  gap_similarity: number;
};
