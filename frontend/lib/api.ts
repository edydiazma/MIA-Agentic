export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---------- Tipos del API ----------
export type Availability = "available" | "away" | "busy";
export type Agent = {
  id: number;
  email: string;
  name: string;
  role: "admin" | "agent";
  availability: Availability;
  is_active: boolean;
};
export type AgentDetail = Agent & { group_ids: number[]; online: boolean };
export type Group = { id: number; name: string; description: string | null };
export type Stage = "lead" | "prospect" | "client" | "lost";
export type Contact = {
  id: number;
  wa_id: string;
  name: string | null;
  email: string | null;
  notes: string | null;
  tags: string[];
  stage: Stage;
  custom_fields: Record<string, string | number | boolean>;
  blocked: boolean;
  blocked_reason: string | null;
  blocked_at: string | null;
  marketing_opt_out: boolean;
  created_at: string | null;
};
export type Conversation = {
  id: number;
  status: "bot" | "human" | "closed";
  contact: Contact;
  channel_id: number;
  assigned_agent: Agent | null;
  group: Group | null;
  handoff_reason: string | null;
  handoff_at: string | null;
  typification: string | null;
  tags: string[];
  ai_summary: string | null;
  ai_sentiment: "positive" | "neutral" | "negative" | null;
  ai_typification: string | null;
  ai_suggestions: AISuggestions | null;
  ai_classified_at: string | null;
  closed_at: string | null;
  ad_source_type: string | null;
  ad_headline: string | null;
  unread_count: number;
  last_message_at: string;
  last_inbound_at: string | null;
  last_message_preview: string | null;
  created_at: string;
};
export type AISuggestions = {
  tags?: string[];
  typification?: { value: string; confidence: number };
  group?: { value: string; group_id: number; confidence: number };
  fields?: { key: string; label: string; value: string | number | boolean; confidence: number; evidence: string }[];
};
export type FieldType = "text" | "long_text" | "number" | "date" | "select" | "boolean" | "email" | "phone";
export type ContactField = {
  id: number;
  key: string;
  label: string;
  type: FieldType;
  options: string[] | null;
  description: string | null;
  ai_extract: boolean;
  agent_editable: boolean;
  position: number;
};
export type FieldChange = {
  id: number;
  field_key: string;
  label: string;
  old_value: string | null;
  new_value: string | null;
  source: "agent" | "ai" | "import";
  agent_name: string | null;
  conversation_id: number | null;
  created_at: string;
};
export type ConversationTag = { name: string; description: string; in_catalog: boolean; count: number };
export type ClassifierMode = "auto" | "suggest" | "off";
export type ClassifierSettings = {
  enabled: boolean;
  cortex_id: number | null; // null = Cortex principal
  instructions: string;
  tags: { name: string; description: string }[];
  typification_criteria: Record<string, string>;
  route_on_handoff: boolean;
  classify_on_close: boolean;
  every_n_messages: number;
  modes: {
    tags: ClassifierMode;
    group: ClassifierMode;
    typification: ClassifierMode;
    fields: "fill_empty" | "overwrite_ai" | "suggest" | "off";
  };
  min_confidence: number;
  max_messages: number;
};
export type ClassificationResult = {
  summary: string;
  sentiment: "positive" | "neutral" | "negative" | null;
  reason: string;
  tags: { name: string; confidence: number }[];
  typification: string | null;
  typification_confidence: number;
  group: string | null;
  group_confidence: number;
  fields: { key: string; value: string; confidence: number; evidence: string }[];
};
export type ClassifyResponse = {
  result?: ClassificationResult;
  skipped?: string;
  latency_ms?: number;
  applied: { tags: string[]; group: string | null; typification: string | null; fields: Record<string, unknown> } | null;
  conversation?: Conversation;
};
export const SENTIMENT_LABEL = { positive: "😊 Positivo", neutral: "😐 Neutral", negative: "😟 Negativo" } as const;

export type Message = {
  id: number;
  conversation_id: number;
  direction: "in" | "out";
  sender_type: "contact" | "bot" | "agent" | "campaign" | "system";
  sender_agent_id: number | null;
  type: string;
  text: string | null;
  media_mime: string | null;
  media_filename: string | null;
  transcript: string | null;
  template_name: string | null;
  has_media: boolean;
  status: string;
  error: string | null;
  created_at: string;
};
/** Agente de IA (ruta /api/bots por compatibilidad). */
export type Bot = {
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
};
export type KnowledgeDoc = {
  id: number;
  bot_ids: number[];
  title: string;
  content: string;
  enabled: boolean;
  source_filename: string | null;
  updated_at: string;
  chars: number;
};
export type Template = {
  name: string;
  language: string;
  status: string;
  category: string;
  header: string | null;
  body: string;
  variables: string[];
  named: boolean;
  supported: boolean;
  unsupported_reason: string | null;
};
export type CampaignStats = {
  total: number;
  sent: number;
  delivered: number;
  read: number;
  failed: number;
  not_delivered: number;
};
export type Campaign = CampaignStats & {
  id: number;
  name: string;
  template_name: string;
  template_language: string;
  params: string[];
  status: "draft" | "running" | "done" | "failed";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};
export type CampaignDetail = Campaign & {
  recipients: { id: number; contact_id: number; wa_id: string; name: string | null; status: string; error: string | null }[];
};
export type AutomationType = "welcome" | "keyword_reply" | "keyword_handoff" | "business_hours" | "inactivity_close";
export type Automation = {
  id: number;
  name: string;
  type: AutomationType;
  config: Record<string, any>;
  enabled: boolean;
  priority: number;
};
export type OutboundWebhook = {
  id: number;
  name: string;
  url: string;
  secret: string;
  events: string[];
  active: boolean;
  consecutive_failures: number;
  last_status: number | null;
  last_error: string | null;
  last_delivery_at: string | null;
};
export type FollowUp = {
  id: number;
  contact_id: number;
  contact_name: string | null;
  wa_id: string;
  conversation_id: number | null;
  agent_id: number;
  agent_name: string;
  due_at: string;
  note: string;
  done: boolean;
  overdue: boolean;
};
export type Appointment = {
  id: number;
  contact_id: number;
  contact_name: string | null;
  wa_id: string;
  conversation_id: number | null;
  agent_id: number | null;
  agent_name: string | null;
  starts_at: string;
  duration_min: number;
  title: string;
  notes: string | null;
  status: "scheduled" | "done" | "cancelled" | "no_show";
  created_by: "bot" | "agent";
};
export type QuickReply = { id: number; shortcut: string; text: string };
export type Resource = { id: number; name: string; mime: string; size: number; created_at: string };
export type Channel = {
  id: number;
  name: string;
  phone_number_id: string;
  display_phone: string | null;
  waba_id?: string | null;
  bot_id: number | null; // agente de IA por defecto del número (default_ai_agent_id)
  has_own_token: boolean;
  token_configured: boolean;
};
export type Alert = {
  id: number;
  severity: "info" | "warning" | "critical";
  layer: string;
  source: "meta" | "system";
  title: string;
  description: string | null;
  ref: string | null;
  created_at: string;
};
export type Integration = {
  key: string;
  name: string;
  category: string;
  description: string;
  connected: boolean;
  last_sync_at: string | null;
  last_error: string | null;
  available: boolean;
  status: string | null;
  /** Pantalla del panel donde se conecta o gestiona. */
  href: string;
};
export type Settings = {
  classifier: ClassifierSettings;
  company: { name: string; timezone: string; website: string; address: string };
  conversations: { typifications: string[]; require_typification: boolean; auto_assign: boolean; sla_minutes: number };
  reports: { sla_target_pct: number };
  appointments: {
    enabled: boolean;
    title: string;
    duration_min: number;
    days: number[];
    start: string;
    end: string;
    capacity: number;
    max_days_ahead: number;
  };
};

// ---------- Sesión ----------
export function getToken(): string | null {
  try {
    return localStorage.getItem("token");
  } catch {
    return null;
  }
}

export function setSession(token: string | null, agent?: Agent) {
  try {
    if (token) {
      localStorage.setItem("token", token);
      if (agent) localStorage.setItem("agent", JSON.stringify(agent));
    } else {
      localStorage.removeItem("token");
      localStorage.removeItem("agent");
    }
  } catch {}
}

export function getAgent(): Agent | null {
  try {
    return JSON.parse(localStorage.getItem("agent") ?? "null");
  } catch {
    return null;
  }
}

// ---------- HTTP ----------
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (res.status === 401) {
    setSession(null);
    if (typeof window !== "undefined" && !location.pathname.startsWith("/login")) location.href = "/login";
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

/** POST/PUT/DELETE con JSON. */
export const send = <T>(path: string, method: "POST" | "PUT" | "DELETE", body?: unknown) =>
  api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

/** Construye ?a=1&b=2 omitiendo valores vacíos. */
export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const mediaUrl = (messageId: number) => `${API_URL}/api/media/${messageId}?token=${getToken() ?? ""}`;
export const resourceUrl = (id: number) => `${API_URL}/api/resources/${id}/file?token=${getToken() ?? ""}`;
export const downloadUrl = (path: string) =>
  `${API_URL}${path}${path.includes("?") ? "&" : "?"}token=${getToken() ?? ""}`;

export function wsUrl(): string {
  return `${API_URL.replace(/^http/, "ws")}/ws?token=${getToken() ?? ""}`;
}

// ---------- Formato ----------
export const fmtDateTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("es", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
export const fmtDate = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString("es", { day: "2-digit", month: "short", year: "numeric" }) : "—";
export const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });
export const fmtNum = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("es"));
export const fmtPct = (n: number | null | undefined) => (n == null ? "—" : `${n.toLocaleString("es")} %`);

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "hace un momento";
  if (s < 3600) return `hace ${Math.floor(s / 60)} min`;
  if (s < 86400) return `hace ${Math.floor(s / 3600)} h`;
  if (s < 172800) return `ayer ${fmtTime(iso)}`;
  return `hace ${Math.floor(s / 86400)} días`;
}

export const contactLabel = (c: { name: string | null; wa_id: string }) => c.name || `+${c.wa_id}`;

export const STAGE_LABEL: Record<Stage, string> = {
  lead: "Lead",
  prospect: "Prospecto",
  client: "Cliente",
  lost: "Perdido",
};
export const STATUS_LABEL: Record<Conversation["status"], string> = { bot: "Bot", human: "Asesor", closed: "Cerrada" };
export const AVAILABILITY_LABEL: Record<Availability, string> = {
  available: "Disponible",
  away: "Ausente",
  busy: "Ocupado",
};

/** Fecha local AAAA-MM-DD (para inputs type=date y rangos de reportes). */
export function isoDay(d: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
export function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return isoDay(d);
}
