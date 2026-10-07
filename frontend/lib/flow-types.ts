// Tipos del editor de flujos (ver docs/flows.md). Contrato del API /api/flows.

export type TriggerType =
  | "inbound_message"
  | "keyword"
  | "handoff"
  | "close"
  | "schedule"
  | "webhook"
  | "manual"
  | "campaign_reply";

export type FlowStatus = "draft" | "active" | "paused" | "archived";
export type EditorMode = "junior" | "advanced";

/** Condición estructurada (bloques de Operadores). Nunca se ejecuta código libre. */
export type Condition =
  | { op: "eq" | "gt" | "lt" | "contains" | "join"; left: Operand; right: Operand }
  | { op: "and" | "or"; left: Condition | null; right: Condition | null }
  | { op: "not" | "length"; value: Operand | Condition | null };
export type Operand = string | number | Condition | null;

export type InputValue = string | number | boolean | string[] | Condition | null;

/**
 * Bloque. `branches`:
 * - if → { then: [...], else: [...] }
 * - repeat → { body: [...] }
 * - switch_reply / ai_decide → una rama por opción (clave = texto de la opción) + "other" (sin coincidencia / tiempo agotado)
 */
export type FlowBlock = {
  id: string;
  type: string;
  inputs: Record<string, InputValue>;
  branches?: Record<string, FlowBlock[]>;
  disabled?: boolean;
};

export type FlowScript = {
  id: string;
  trigger: { type: TriggerType | string; config: Record<string, InputValue> };
  blocks: FlowBlock[];
  position?: { x: number; y: number };
};

export type FlowVariable = { name: string; type: "text" | "number" | "boolean"; default: string | number | boolean | null };

export type FlowDefinition = {
  schema_version: 1;
  variables: FlowVariable[];
  scripts: FlowScript[];
};

export type Flow = {
  id: number;
  name: string;
  description: string | null;
  trigger_type: TriggerType;
  trigger_config?: Record<string, InputValue>;
  status: FlowStatus;
  editor_mode: EditorMode;
  priority: number;
  current_version_id: number | null;
  current_version: { version: number; created_at: string } | null;
  updated_at: string;
};

export type FlowDetail = Flow & { definition: FlowDefinition | null; versions_count: number };

export type FlowVersion = {
  id: number;
  version: number;
  change_note: string | null;
  created_by_ai: boolean;
  ai_prompt: string | null;
  created_at: string;
};
/** GET /api/flows/{id}/versions/{version_id} (necesario para "cargar versión anterior"). */
export type FlowVersionDetail = FlowVersion & { definition: FlowDefinition };

export type FlowError = { path: string; message: string; block_id?: string };

export type FlowTestResult = {
  steps: { block_id: string; type: string; status: string; output?: unknown; error?: string | null }[];
  messages: { text: string }[];
  waiting: boolean;
};

export type FlowRun = {
  id: number;
  flow_id: number;
  flow_version_id: number;
  conversation_id: number | null;
  contact_id: number | null;
  trigger_type: string;
  status: "running" | "waiting" | "succeeded" | "failed" | "cancelled";
  error: string | null;
  resume_at: string | null;
  started_at: string;
  finished_at: string | null;
};
export type FlowRunStep = {
  id: number;
  block_id: string;
  block_type: string;
  status: "ok" | "error" | "skipped" | "waiting";
  input: unknown;
  output: unknown;
  error: string | null;
  latency_ms: number | null;
  created_at: string;
};
export type FlowRunDetail = FlowRun & { steps: FlowRunStep[]; context?: Record<string, unknown> };

// ---- Catálogo ----
export type InputType =
  | "text"
  | "number"
  | "select"
  | "tag"
  | "group"
  | "agent"
  | "field"
  | "typification"
  | "template"
  | "resource"
  | "ai_agent"
  | "buttons"
  | "options"
  | "condition"
  | "duration";

export type CatalogInput = {
  name: string;
  label: string;
  type: InputType;
  required: boolean;
  options?: { value: string; label: string }[];
};

export type BlockShape = "hat" | "stack" | "c" | "reporter" | "cap";

export type CatalogBlock = {
  type: string;
  category: string;
  label: string;
  icon: string;
  junior: boolean;
  shape: BlockShape;
  inputs: CatalogInput[];
  /** ["then","else"] (if), ["body"] (repeat) u ["options"] (una rama por opción + "other"). */
  branches?: string[];
};

export type FlowCatalog = {
  categories: { key: string; label: string; color: string }[];
  blocks: CatalogBlock[];
};
