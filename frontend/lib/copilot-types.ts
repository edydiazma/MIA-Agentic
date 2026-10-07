/** Copiloto del asesor y asistente del supervisor (docs/data-model.md §19.3). */

export type CopilotKind = "reply" | "draft" | "next_action" | "summary" | "rewrite" | "translate";
export type CopilotStatus = "shown" | "accepted" | "edited" | "dismissed" | "expired";

export type NextActionKind =
  | "send_product"
  | "offer_appointment"
  | "send_template"
  | "ask_field"
  | "handoff"
  | "create_deal"
  | "move_stage"
  | "none";

export type CopilotSuggestion = {
  id: number;
  kind: CopilotKind;
  status: CopilotStatus;
  message_id: number | null;
  agent_id: number | null;
  latency_ms: number | null;
  created_at: string;
  content: {
    // reply
    options?: string[];
    text?: string | null;
    rejected?: { text: string; flags: string[] }[];
    // next_action
    action?: NextActionKind;
    label?: string;
    reason?: string;
    confidence?: number | null;
    payload?: Partial<Record<"text" | "sku" | "product_name" | "field" | "pipeline" | "stage" | "template" | "group" | "date", string>>;
    flags?: string[];
    instruction?: string | null;
  };
};

export type CopilotOverview = {
  reply: CopilotSuggestion | null;
  next_action: CopilotSuggestion | null;
  fresh: boolean;
  pending: boolean;
  enabled: boolean;
  mode: "auto" | "on_demand";
  handoff_summary: string | null;
  handoff_summary_at: string | null;
  summary: string | null;
  summary_at: string | null;
};

export type ExecuteResult = {
  action: NextActionKind;
  client_action: "insert_text" | "open_template" | "open_transfer" | null;
  insert_text: string | null;
  deal_id?: number;
  interaction_product_id?: number;
};

export type RewriteMode = "shorter" | "friendlier" | "formal" | "fix_grammar" | "translate";
export const REWRITE_LABELS: Record<RewriteMode, string> = {
  shorter: "Más corto",
  friendlier: "Más cálido",
  formal: "Más formal",
  fix_grammar: "Corregir ortografía",
  translate: "Traducir al inglés",
};

export type CopilotSettings = {
  enabled: boolean;
  suggestions: "auto" | "on_demand";
  max_suggestions: number;
  max_per_conversation_per_hour: number;
  debounce_seconds: number;
  history_messages: number;
  next_action: boolean;
  handoff_summary: boolean;
  close_summary: boolean;
  assistant: boolean;
  reply_cortex_id: number | null;
  summary_cortex_id: number | null;
  assistant_cortex_id: number | null;
};

export type AdoptionRates = {
  shown: number;
  accepted: number;
  edited: number;
  dismissed: number;
  adoption_pct: number | null;
  accepted_unchanged_pct: number | null;
  avg_latency_ms: number | null;
};

export type CopilotReport = {
  totals: AdoptionRates;
  by_agent: (AdoptionRates & { agent_id: number; name: string | null })[];
  by_kind: (AdoptionRates & { kind: CopilotKind })[];
  series: { day: string; shown: number; used: number; dismissed: number }[];
};

export type AssistantChart = { title: string; data: ({ day: string } & Record<string, number | string>)[]; keys: string[] };
export type AssistantMessage = {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_calls: { name: string; input: Record<string, unknown>; ok: boolean }[] | null;
  charts: AssistantChart[] | null;
  created_at: string;
};
export type AssistantThread = { id: number; title: string | null; created_at: string; updated_at: string };

export const KIND_LABELS: Record<CopilotKind, string> = {
  reply: "Respuestas sugeridas",
  draft: "Borradores",
  next_action: "Siguiente acción",
  summary: "Resúmenes",
  rewrite: "Reescrituras",
  translate: "Traducciones",
};
