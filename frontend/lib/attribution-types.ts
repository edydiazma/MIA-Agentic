// Tipos de atribución web / Click to WA y conversiones (espejo de app/routers/attribution.py y conversions.py).

export type AttributionChannel =
  | "meta_ctwa"
  | "google_ads"
  | "meta_ads_web"
  | "paid_other"
  | "organic_web"
  | "campaign"
  | "direct";

export const CHANNEL_LABEL: Record<AttributionChannel, string> = {
  meta_ctwa: "Click to WhatsApp (Meta)",
  google_ads: "Google Ads",
  meta_ads_web: "Meta Ads (web)",
  paid_other: "Otros pagados",
  organic_web: "Web orgánico",
  campaign: "Campaña saliente",
  direct: "Directo",
};

export const channelLabel = (c: string | null | undefined) =>
  c ? (CHANNEL_LABEL as Record<string, string>)[c] ?? c : "—";

export type TrackingSite = {
  id: number;
  name: string;
  public_key: string;
  allowed_domains: string[];
  channel_id: number | null;
  wa_prefill: string;
  is_active: boolean;
  created_at: string;
  script_url: string;
  snippet: string;
  gtm_snippet: string;
  wa_link: string;
  sessions_7d: number;
  clicks_7d: number;
};

export type TrackingSiteIn = {
  name: string;
  allowed_domains: string[];
  channel_id: number | null;
  wa_prefill: string;
  is_active: boolean;
};

export type Connection = {
  provider: "google_ads" | "meta";
  connected: boolean;
  status: string | null;
  external_account_id: string | null;
  settings: Record<string, string | null>;
  last_error: string | null;
  has_token: boolean;
  configured: boolean;
  developer_token?: boolean;
  server_token?: boolean;
};

export type ConversionTrigger = "typification" | "stage_client" | "appointment_booked" | "deal_won";

export const TRIGGER_LABEL: Record<ConversionTrigger, string> = {
  typification: "Conversación cerrada con tipificación",
  stage_client: "Contacto pasa a etapa Cliente",
  appointment_booked: "Cita agendada",
  deal_won: "Negocio ganado",
};

export type ConversionAction = {
  id: number;
  name: string;
  trigger: ConversionTrigger;
  typification_id: number | null;
  typification: string | null;
  value: number | null;
  currency: string;
  google_ads: { customer_id: string; conversion_action_id: string } | null;
  meta: { dataset_id: string; event_name: string; whatsapp_business_account_id: string | null } | null;
  is_active: boolean;
  events_30d: number;
};

export type ConversionActionIn = Omit<ConversionAction, "id" | "typification" | "events_30d">;

export type ActionOptions = {
  typifications: { id: number; name: string }[];
  triggers: ConversionTrigger[];
  meta_events: string[];
};

export type UploadStatus = "pending" | "sent" | "failed" | "skipped";
export type Destination = "google_ads" | "meta_capi";

export const DESTINATION_LABEL: Record<Destination, string> = { google_ads: "Google Ads", meta_capi: "Meta CAPI" };
export const UPLOAD_STATUS: Record<UploadStatus, { label: string; tone: "ok" | "warn" | "bad" | "neutral" }> = {
  pending: { label: "Pendiente", tone: "warn" },
  sent: { label: "Enviada", tone: "ok" },
  failed: { label: "Falló", tone: "bad" },
  skipped: { label: "Omitida", tone: "neutral" },
};

export type ConversionUpload = {
  id: number;
  event_id: number;
  destination: Destination;
  status: UploadStatus;
  attempts: number;
  error: string | null;
  next_attempt_at: string;
  sent_at: string | null;
  action: string;
  contact: string;
  conversation_id: number | null;
  value: number | null;
  currency: string;
  occurred_at: string;
  channel: string | null;
};

export type WebTrafficReport = {
  totals: {
    sessions: number;
    page_views: number;
    wa_clicks: number;
    conversations: number;
    click_rate_pct: number | null;
    conversation_rate_pct: number | null;
    paid_sessions: number;
  };
  series: { day: string; sessions: number; clicks: number; conversations: number }[];
  by_source: {
    source: string;
    medium: string;
    campaign: string | null;
    sessions: number;
    clicks: number;
    conversations: number;
    click_rate_pct: number | null;
  }[];
  top_landings: { path: string; sessions: number }[];
};

type Bucket = { conversations: number; sales: number; conversions: number; value: number };

export type AttributionReport = {
  totals: { conversations: number; sales: number; conversions: number; value: number; attributed_pct: number | null };
  by_channel: (Bucket & { channel: string; label: string; closed: number; sale_rate_pct: number | null })[];
  by_campaign: (Bucket & { channel: string; campaign: string; closed: number; sale_rate_pct: number | null })[];
  uploads: Record<UploadStatus, number>;
};

export type ClickToWaGoogleReport = {
  totals: {
    wa_clicks: number;
    conversations: number;
    sales: number;
    sale_rate_pct: number | null;
    value: number;
    uploads_sent: number;
    uploads_failed: number;
    uploads_pending: number;
  };
  series: { day: string; conversations: number; sales: number }[];
  by_campaign: (Bucket & { campaign: string })[];
  by_keyword: (Bucket & { keyword: string })[];
};

export type ConversationAttribution = {
  channel: string;
  label: string;
  matched_by: string;
  utm_source: string | null;
  utm_medium: string | null;
  utm_campaign: string | null;
  utm_term: string | null;
  gclid: string | null;
  ctwa_clid: string | null;
  ad_id: string | null;
  landing_url: string | null;
  created_at: string;
} | null;
