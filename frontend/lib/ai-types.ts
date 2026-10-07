import type { AgentAdvanced } from "./agent-config-types";
/**
 * Tipos de los módulos de IA (Cortex, agentes, memoria, mejor vendedor, catálogo, edición JSON con IA).
 * Espejo de backend/app/routers/{cortex,bots,memory,seller,catalog,json_edit}.py — si el backend cambia,
 * se ajusta solo este archivo.
 */

export type AIProvider = "anthropic" | "openai" | "openai_compatible" | "azure_openai";
export const PROVIDER_LABEL: Record<AIProvider, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI",
  openai_compatible: "Compatible con OpenAI",
  azure_openai: "Azure OpenAI",
};
export const MODEL_SUGGESTIONS: Record<AIProvider, string[]> = {
  anthropic: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"],
  openai: ["gpt-4.1", "gpt-4.1-mini"],
  openai_compatible: [],
  azure_openai: [],
};

export type HealthState = "closed" | "open" | "half_open";
export type ConnectionHealth = {
  state: HealthState;
  consecutive_failures: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
};

export type AIConnection = {
  id: number;
  name: string;
  provider: AIProvider;
  model: string;
  base_url: string | null;
  has_api_key: boolean;
  uses_server_key?: boolean;
  default_params: Record<string, unknown>;
  timeout_ms: number;
  input_cost_per_mtok: number | null;
  output_cost_per_mtok: number | null;
  is_active: boolean;
  health?: ConnectionHealth | null;
};
export type AIConnectionIn = Omit<AIConnection, "id" | "has_api_key" | "health"> & {
  api_key?: string; // solo escritura; vacío = conservar
  clear_api_key?: boolean;
};

export type CortexStrategy = "failover" | "lowest_latency" | "weighted";
export type CortexPurpose = "chat" | "classification" | "learning" | "flow" | "json_edit" | "qa" | "agent_test" | "onboarding" | "golden" | "copilot" | "assistant" | "any";
export type CortexMember = {
  connection_id: number;
  position: number;
  weight: number;
  timeout_ms: number | null;
  // solo lectura (respuesta del backend)
  connection_name?: string;
  provider?: string;
  model?: string;
  is_active?: boolean;
  health?: ConnectionHealth;
};
export type CortexValidation = { min_chars?: number | null; max_chars?: number | null; banned_phrases?: string[] };
export type Cortex = {
  id: number;
  name: string;
  description: string | null;
  purpose: CortexPurpose;
  strategy: CortexStrategy;
  max_latency_ms: number | null;
  max_attempts: number;
  circuit_breaker_failures: number;
  circuit_breaker_cooldown_s: number;
  validation: CortexValidation;
  is_active: boolean;
  members: CortexMember[];
};
export type CortexIn = Omit<Cortex, "id">;

export type CallStatus = "ok" | "error" | "timeout" | "slow" | "invalid" | "refused";
export type Attempt = { connection: string; model: string; status: CallStatus; latency_ms: number; error: string | null };
export type CortexTestResult = { ok: boolean; answer?: unknown; attempts: Attempt[]; error?: string | null };
export type AICall = {
  id: number;
  created_at: string;
  cortex_id: number | null;
  connection_id: number | null;
  connection_name?: string | null;
  purpose: string;
  conversation_id: number | null;
  attempt: number;
  status: CallStatus;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  error: string | null;
  fallback_from_call_id: number | null;
};

export type AIAgent = {
  id: number;
  name: string;
  description: string | null;
  enabled: boolean;
  cortex_id: number | null;
  system_prompt: string;
  handoff_message: string;
  use_knowledge: boolean;
  use_memory: boolean;
  use_customer_memory: boolean;
  use_catalog: boolean;
  use_appointments: boolean;
  channel_ids: number[];
} & AgentAdvanced;
export type AIAgentIn = Omit<AIAgent, "id" | "channel_ids">;

export type MemoryKind = "faq" | "objection" | "winning_response" | "fact" | "policy" | "insight";
export const MEMORY_KIND_LABEL: Record<MemoryKind, string> = {
  faq: "Pregunta frecuente",
  objection: "Objeción",
  winning_response: "Respuesta ganadora",
  fact: "Dato del negocio",
  policy: "Política",
  insight: "Hallazgo",
};
export type MemoryStatus = "pending" | "approved" | "rejected";
export type MemoryItem = {
  id: number;
  kind: MemoryKind;
  title: string;
  content: string;
  status: MemoryStatus;
  source: "learned" | "manual";
  confidence: number | null;
  evidence_conversation_ids: number[];
  run_id: number | null;
  reviewed_by: number | null;
  reviewed_at: string | null;
  created_at: string;
};
export type LearningRun = {
  id: number;
  type: "memory" | "seller";
  status: "running" | "done" | "failed";
  params: Record<string, unknown>;
  stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
};

export type SellerRanking = {
  agent_id: number;
  name: string;
  closed: number;
  sales: number;
  conversion_pct: number | null;
};
export type Playbook = {
  persona?: string;
  tone?: string;
  sales_process?: { stage: string; goal: string; tactics: string[] }[];
  discovery_questions?: string[];
  objections?: { objection: string; response: string }[];
  closing_techniques?: string[];
  do?: string[];
  dont?: string[];
  example_phrases?: string[];
};
export type SellerProfile = {
  id: number;
  name: string;
  status: "draft" | "applied" | "archived";
  source_agent_ids: number[];
  stats: Record<string, unknown> | null;
  playbook: Playbook;
  system_prompt: string;
  ai_agent_id: number | null;
  run_id: number | null;
  created_at: string;
};

export type Product = {
  id: number;
  sku: string;
  name: string;
  description: string | null;
  category: string | null;
  brand: string | null;
  price: number | null;
  sale_price: number | null;
  currency: string;
  stock: number | null;
  available: boolean;
  url: string | null;
  image_url: string | null;
  attributes: Record<string, string> | null;
  source: string;
  in_meta_catalog: boolean;
  updated_at?: string;
};
export type ProductIn = Omit<Product, "id" | "source" | "in_meta_catalog" | "updated_at">;
export type ProductPage = { total: number; items: Product[] };
export type CatalogSettings = {
  currency: string;
  meta_catalog_id: string;
  send_as_catalog_message: boolean;
  feed_url: string;
  feed_format: "csv" | "json";
  feed_interval_hours: number;
  last_sync: Record<string, unknown> | null;
};
export type SyncRun = {
  id: number;
  source: string;
  status: "running" | "done" | "failed";
  stats: Record<string, number> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
};
export type SyncResult = { created: number; updated: number; invalid: number; disabled?: number };

export type JsonEntityType = "setting" | "ai_agent" | "cortex" | "automation" | "flow" | "classifier";
export type DiffRow = { path: string; before: unknown; after: unknown };
export type JsonEditProposal = {
  current?: unknown;
  proposal: unknown;
  summary?: string;
  diff: DiffRow[];
  valid: boolean;
  errors: string[];
  ai_call_ids?: number[];
  attempts?: Attempt[];
};
export type JsonDocument = { entity_type: string; entity_id: string; document: unknown; schema?: unknown };
export type Revision = {
  id: number;
  entity_type: string;
  entity_id: string;
  revision: number;
  document?: unknown;
  source: "human" | "ai" | "import" | "system";
  ai_prompt: string | null;
  ai_call_id?: number | null;
  created_by: number | null;
  created_at: string;
};
export type MemoryList = { total: number; counts: Record<string, number>; kinds?: Record<string, string>; items: MemoryItem[] };
export type ProductList = { total: number; categories: string[]; items: Product[] };
export type SearchHit = { product: Product; as_seen_by_ai: string };

export type AIReportRow = {
  connection_id: number | null;
  name: string;
  provider: string | null;
  model: string | null;
  purpose: string;
  state: HealthState;
  calls: number;
  errors: number;
  slow: number;
  fallbacks: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  error_pct: number | null;
};
export type AIReport = {
  totals: {
    calls: number;
    errors: number;
    slow: number;
    fallbacks: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    error_pct: number | null;
  };
  by_connection: AIReportRow[];
  series: ({ day: string } & Record<string, number | string>)[];
};

export const HEALTH_LABEL: Record<HealthState, string> = { closed: "Operativa", open: "En pausa", half_open: "Probando" };
export const STATUS_TONE: Record<CallStatus, "ok" | "warn" | "bad"> = {
  ok: "ok",
  slow: "warn",
  timeout: "bad",
  error: "bad",
  invalid: "warn",
  refused: "warn",
};
