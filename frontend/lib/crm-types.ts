// Negocios (deals) e integraciones CRM (HubSpot / Salesforce).

export type DealStatus = "open" | "won" | "lost";
export type PipelineStage = { key: string; label: string };
export type Pipeline = { label: string; stages: PipelineStage[] };
export type PipelinesConfig = { pipelines: Record<string, Pipeline>; currency: string };

export type Deal = {
  id: number;
  name: string;
  contact: { id: number; name: string | null; wa_id: string | null };
  conversation_id: number | null;
  owner: { id: number; name: string } | null;
  amount: number | null;
  currency: string;
  pipeline: string;
  stage: string;
  status: DealStatus;
  source: string;
  expected_close: string | null;
  closed_at: string | null;
  lost_reason: string | null;
  created_at: string;
  updated_at: string;
  crm_links: { provider: CRMProvider; remote_id: string }[];
};

export type DealBoard = {
  pipeline: string;
  label: string;
  stages: PipelineStage[];
  columns: Record<string, Deal[]>;
  totals: Record<string, number>;
  currency: string;
};

export const DEAL_STATUS_LABEL: Record<DealStatus, string> = { open: "Abierto", won: "Ganado", lost: "Perdido" };
export const DEAL_STATUS_TONE: Record<DealStatus, "info" | "ok" | "bad"> = { open: "info", won: "ok", lost: "bad" };

export const fmtMoney = (n: number | null | undefined, currency = "COP") =>
  n == null
    ? "—"
    : n.toLocaleString("es", { style: "currency", currency, maximumFractionDigits: currency === "COP" ? 0 : 2 });

// ---------- Integraciones ----------
export type CRMProvider = "hubspot" | "salesforce" | "zoho" | "odoo";
export type ConnectionStatus = "connected" | "error" | "disconnected";
export type PushContacts = "all" | "with_deal" | "none";

export type CRMStatus = {
  provider: CRMProvider;
  label: string;
  configured: boolean;
  connected: boolean;
  status: ConnectionStatus | null;
  id?: number;
  external_account_id?: string | null;
  instance_url?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
  sync_enabled?: boolean;
  settings?: {
    contact_object: "Contact" | "Lead" | null;
    push_contacts: PushContacts | null;
    push_notes: boolean | null;
    last_pull_at: string | null;
  };
  via_oauth?: boolean;
  connected_at?: string;
  outbox?: { pending: number; sent: number; skipped: number; failed: number };
};

export type MappingDirection = "push" | "pull" | "both";
export type CRMMapping = {
  id?: number;
  object: "contact" | "deal";
  local_field: string;
  remote_property: string;
  direction: MappingDirection;
  transform: { map?: Record<string, string> } | null;
};
export type MappingsResponse = {
  mappings: CRMMapping[];
  local_fields: { contact: string[]; deal: string[] };
  defaults: CRMMapping[];
};

export type OutboxRow = {
  id: number;
  entity_type: "contact" | "deal" | "note";
  entity_id: number;
  operation: string;
  status: "pending" | "sent" | "skipped" | "failed";
  attempts: number;
  error: string | null;
  next_attempt_at: string;
  created_at: string;
  sent_at: string | null;
};

export const DIRECTION_LABEL: Record<MappingDirection, string> = {
  push: "Plataforma → CRM",
  pull: "CRM → Plataforma",
  both: "Ambos sentidos",
};
export const OUTBOX_STATUS_LABEL: Record<OutboxRow["status"], string> = {
  pending: "Pendiente",
  sent: "Enviado",
  skipped: "Sin cambios",
  failed: "Falló",
};
export const ENTITY_LABEL: Record<OutboxRow["entity_type"], string> = { contact: "Contacto", deal: "Negocio", note: "Nota" };
