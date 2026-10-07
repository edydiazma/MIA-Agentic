// Segmentos y journeys de marketing (docs/data-model.md §21.1)

export type FieldType = "text" | "enum" | "number" | "date" | "ts" | "bool" | "array";
export type SegmentField = {
  key: string;
  label: string;
  group: string;
  type: FieldType;
  ops: string[];
  options: string[] | null;
};
export type RuleLeaf = { field: string; op: string; value?: unknown };
export type RuleNode = { all: RuleNode[] } | { any: RuleNode[] } | { not: RuleNode } | RuleLeaf;

export type Segment = {
  id: number;
  name: string;
  description: string | null;
  kind: "dynamic" | "static";
  definition: RuleNode | Record<string, never>;
  member_count: number;
  last_computed_at: string | null;
  refresh_minutes: number;
  created_at: string;
  updated_at: string;
  journeys: { id: number; name: string; status: string }[];
};
export type SegmentPreview = {
  count: number;
  sample: { id: number; name: string | null; wa_id: string | null; email: string | null; stage: string }[];
};

export const OP_LABELS: Record<string, string> = {
  eq: "es",
  neq: "no es",
  in: "es uno de",
  contains: "contiene",
  gte: "mayor o igual a",
  lte: "menor o igual a",
  between: "está entre",
  within_days: "en los próximos (días)",
  before_days: "en los últimos (días)",
  exists: "tiene un valor",
};

export type StepType =
  | "send_template"
  | "send_text"
  | "send_email"
  | "wait"
  | "wait_until"
  | "branch"
  | "split"
  | "update_contact"
  | "add_tag"
  | "create_deal"
  | "move_stage"
  | "notify_agent"
  | "start_flow"
  | "exit";

export type Step = {
  id: string;
  type: StepType;
  config?: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  next?: string | null;
  branches?: Record<string, string | null>;
  position?: Record<string, number>;
};
export type Definition = { start: string; steps: Step[] };

export type JourneyEntry = {
  type: "segment_enter" | "segment_member" | "event" | "date_field" | "manual" | "api";
  segment_id?: number | null;
  event?: string | null;
  filter?: Record<string, unknown>;
  field?: string | null;
  offset_days?: number | null;
  at_time?: string | null;
};
export type JourneySettings = {
  reentry?: "never" | "after_days" | "always";
  reentry_days?: number | null;
  frequency_cap?: { per_day?: number | null; per_week?: number | null };
  quiet_hours?: { from: string; to: string; timezone?: string | null } | null;
  channels_priority?: ("whatsapp_cloud" | "email" | "webchat")[];
  require_consent?: "marketing" | null;
  goal?: { event: string; window_days?: number | null; pipeline?: string | null; stage?: string | null } | null;
};
export type Journey = {
  id: number;
  name: string;
  description: string | null;
  status: "draft" | "active" | "paused" | "archived";
  entry: JourneyEntry;
  settings: JourneySettings;
  current_version_id: number | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  stats: Partial<Record<"enrolled" | "active" | "waiting" | "completed" | "goal_met" | "exited" | "failed", number>>;
};
export type JourneyDetail = {
  journey: Journey;
  version: { id: number; version: number; definition: Definition } | null;
  errors: string[];
  versions: { id: number; version: number; change_note: string | null; created_at: string; current: boolean }[];
};
export type JourneyMeta = {
  step_types: Record<StepType, string>;
  events: Record<string, string>;
  goals: string[];
  entry_types: string[];
  branch_conditions: string[];
};
export type JourneyReport = {
  journey_id: number;
  totals: Journey["stats"];
  conversion_metric: string;
  steps: ({ id: string; type: StepType; label: string } & Record<
    "sent" | "delivered" | "read" | "replied" | "clicked" | "failed" | "skipped" | "waited" | "branched",
    number
  >)[];
  ab: {
    step_id: string;
    metric: string;
    variants: {
      key: string;
      enrolled: number;
      conversions: number;
      rate: number;
      lift?: number | null;
      z?: number | null;
      p_value?: number | null;
      significant?: boolean;
    }[];
  }[];
};
export type Enrollment = {
  id: number;
  contact_id: number;
  contact_name: string | null;
  contact_phone: string | null;
  status: string;
  current_step: string | null;
  variant: string | null;
  next_run_at: string | null;
  exit_reason: string | null;
  enrolled_at: string;
  finished_at: string | null;
  source: string | null;
};

export const ENTRY_LABELS: Record<JourneyEntry["type"], string> = {
  segment_enter: "Cuando entra a un segmento",
  segment_member: "Miembros de un segmento (actuales y nuevos)",
  event: "Cuando ocurre un evento",
  date_field: "En una fecha del cliente",
  manual: "Manual (desde el panel)",
  api: "Por API (/v1/journeys/{id}/enroll)",
};
export const GOAL_LABELS: Record<string, string> = {
  replied: "Respondió",
  deal_won: "Negocio ganado",
  appointment_booked: "Agendó cita",
  purchase: "Compró (pedido pagado)",
  stage: "Llegó a una etapa",
};
export const STATUS_LABELS: Record<string, string> = {
  draft: "Borrador",
  active: "Activo",
  paused: "En pausa",
  archived: "Archivado",
  waiting: "Esperando",
  completed: "Completó",
  goal_met: "Cumplió la meta",
  exited: "Salió",
  failed: "Falló",
};
export const STATUS_TONE = { draft: "neutral", active: "ok", paused: "warn", archived: "neutral" } as const;
