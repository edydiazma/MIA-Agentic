// Tipos de atribución web / Click to WA y conversiones (espejo de app/routers/attribution.py y conversions.py).

export type AttributionChannel =
  | "meta_ctwa"
  | "google_ads"
  | "meta_ads_web"
  | "paid_other"
  | "organic_web"
  | "offline"
  | "campaign"
  | "direct";

export const CHANNEL_LABEL: Record<AttributionChannel, string> = {
  meta_ctwa: "Click to WhatsApp (Meta)",
  google_ads: "Google Ads",
  meta_ads_web: "Meta Ads (web)",
  paid_other: "Otros pagados",
  organic_web: "Web orgánico",
  offline: "Offline (QR, SMS)",
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

export type AttributionLinkRef = { id: number; name: string; slug: string; platform: LinkPlatform };

export type AttributionTouch = {
  id: number;
  occurred_at: string;
  channel: string;
  label: string;
  matched_by: string;
  is_first: boolean;
  link: AttributionLinkRef | null;
  utm_source: string | null;
  utm_medium: string | null;
  utm_campaign: string | null;
  ad_id: string | null;
  gclid: string | null;
};

export type ConversationAttribution = {
  channel: string;
  label: string;
  matched_by: string;
  utm_source: string | null;
  utm_medium: string | null;
  utm_campaign: string | null;
  utm_content?: string | null;
  utm_term: string | null;
  gclid: string | null;
  ctwa_clid: string | null;
  ad_id: string | null;
  landing_url: string | null;
  created_at: string;
  link?: AttributionLinkRef | null;
  campaign_id?: string | null;
  campaign_name?: string | null;
  ad_group_name?: string | null;
  ad_name?: string | null;
  keyword?: string | null;
  enrichment_status?: "pending" | "done" | "failed" | "skipped";
  touch_count?: number;
  last_touch_at?: string | null;
  touches?: AttributionTouch[];
} | null;

export const MATCHED_BY_LABEL: Record<string, string> = {
  ctwa_referral: "Anuncio Click to WhatsApp",
  ref_code: "Código de la visita web",
  trigger_text: "Texto del mensaje disparador",
  campaign: "Campaña de plantilla",
  none: "Sin señal de origen",
};

// ---- Mensajes disparadores (wa_links) ----
export type LinkPlatform = "meta_ads" | "google_ads" | "web" | "social" | "email" | "qr" | "sms" | "other";

export const PLATFORM_LABEL: Record<LinkPlatform, string> = {
  meta_ads: "Meta Ads (Click to WhatsApp)",
  google_ads: "Google Ads",
  web: "Sitio web",
  social: "Redes sociales (orgánico)",
  email: "Correo",
  qr: "QR / impresos",
  sms: "SMS",
  other: "Otro",
};

export type WaLinkIn = {
  name: string;
  trigger_text: string;
  platform: LinkPlatform;
  slug: string | null;
  channel_id: number | null;
  append_ref: boolean;
  utm_source: string | null;
  utm_medium: string | null;
  utm_campaign: string | null;
  utm_content: string | null;
  utm_term: string | null;
  meta_ad_ids: string[];
  google_campaign_ids: string[];
  tags: string[];
  group_id: number | null;
  flow_id: number | null;
  is_active: boolean;
};

export type WaLink = Omit<WaLinkIn, "slug"> & {
  id: number;
  slug: string;
  short_url: string;
  direct_url: string;
  google_final_url_suffix: string;
  stats: { clicks: number; conversations: number; conversions: number; conversion_value: number };
  created_at: string;
  updated_at: string;
};

export type LinkTestMatch = { key: string; ref_code: string | null; link: { id: number; name: string; slug: string } | null };
export type MetaAd = { id: string; name: string; status: string; campaign_id: string | null; campaign_name: string | null; adset_name: string | null };
export type GoogleCampaign = { id: string; name: string; status: string };

type LinkMetrics = {
  clicks: number;
  conversations: number;
  first_touches: number;
  retouches: number;
  conversions: number;
  conversion_value: number;
};

export type WaLinksReport = {
  totals: LinkMetrics;
  links: (LinkMetrics & {
    link_id: number;
    name: string;
    slug: string | null;
    platform: LinkPlatform | null;
    is_active: boolean;
    click_to_chat_pct: number | null;
    conversion_rate_pct: number | null;
  })[];
  series: ({ day: string } & LinkMetrics)[];
};
