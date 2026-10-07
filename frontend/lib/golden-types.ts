/**
 * Registro maestro del cliente (docs/data-model.md §15) y rastreo por anuncio (§16).
 * Espejo de los contratos de /api/golden/*, /api/contacts/{id}/golden, /api/reports/ads y /api/ads/*.
 */
import { api } from "@/lib/api";

// ---------- Llaves y registro maestro ----------
export type KeySource =
  | "whatsapp"
  | "channel"
  | "ai_conversation"
  | "ai_document"
  | "agent"
  | "import"
  | "api"
  | "crm"
  | "flow"
  | "form";

export const KEY_SOURCE_LABEL: Record<KeySource, string> = {
  whatsapp: "WhatsApp",
  channel: "Canal",
  ai_conversation: "IA conversación",
  ai_document: "IA documento",
  agent: "Asesor",
  import: "Importado",
  api: "API",
  crm: "CRM",
  flow: "Flujo",
  form: "Formulario",
};

export type KeyType = {
  id: number;
  organization_id?: number | null;
  key: string;
  label: string;
  normalizer: string;
  is_identifier: boolean;
  is_sensitive: boolean;
  multi: boolean;
  ai_extract: boolean;
  ai_hint: string | null;
  position: number;
  archived_at?: string | null;
};

export const NORMALIZERS = ["phone", "email", "username", "document", "plate", "vin", "name", "date", "address", "text"] as const;
export const NORMALIZER_LABEL: Record<string, string> = {
  phone: "Teléfono",
  email: "Correo",
  username: "Usuario de red",
  document: "Documento",
  plate: "Placa",
  vin: "VIN",
  name: "Nombre",
  date: "Fecha",
  address: "Dirección",
  text: "Texto",
};

export type ContactKey = {
  id: number;
  key_type: string;
  label?: string | null;
  subtype: string | null;
  value: string;
  value_normalized: string;
  rank: "primary" | "secondary";
  status: "active" | "superseded" | "rejected";
  verified: boolean;
  confidence: number | null;
  source: KeySource;
  evidence: string | null;
  conversation_id: number | null;
  message_id: number | null;
  first_seen_at: string;
  last_seen_at: string;
  seen_count: number;
  masked?: boolean;
};

export type Vehicle = {
  id: number;
  plate: string | null;
  vin: string | null;
  make: string | null;
  model: string | null;
  version: string | null;
  year: number | null;
  color: string | null;
  fuel: string | null;
  mileage_km: number | null;
  relation: "owner" | "driver" | "interested" | "previous_owner";
  status: "active" | "sold" | "inactive";
  insurance_due: string | null;
  inspection_due: string | null;
  warranty_until: string | null;
  next_service_at: string | null;
  source: string;
  confidence: number | null;
  attributes?: Record<string, unknown>;
};

export const RELATION_LABEL: Record<Vehicle["relation"], string> = {
  owner: "Propietario",
  driver: "Conductor",
  interested: "Interesado",
  previous_owner: "Propietario anterior",
};

export type Consent = {
  id?: number;
  consent_type: string;
  granted: boolean;
  policy_version: string | null;
  source: string;
  recorded_at: string;
  revoked_at: string | null;
  evidence: string | null;
};

export const CONSENT_LABEL: Record<string, string> = {
  habeas_data: "Habeas data (tratamiento de datos)",
  terms: "Términos y condiciones",
  marketing: "Marketing",
  data_sharing: "Compartir datos con terceros",
  call_recording: "Grabación de llamadas",
};

export type Extraction = {
  id: number;
  source_kind: "conversation" | "document" | "image" | "audio";
  document_type: string | null;
  status: "pending" | "done" | "failed" | "skipped";
  keys_added: number;
  created_at: string;
};

export const SOURCE_KIND_LABEL: Record<Extraction["source_kind"], string> = {
  conversation: "Conversación",
  document: "Documento",
  image: "Imagen",
  audio: "Audio",
};

export const DOCUMENT_TYPE_LABEL: Record<string, string> = {
  cedula: "Cédula",
  pasaporte: "Pasaporte",
  licencia: "Licencia de conducción",
  tarjeta_propiedad: "Tarjeta de propiedad",
  soat: "SOAT",
  rtm: "Revisión técnico-mecánica",
  factura: "Factura",
  rut: "RUT",
  otro: "Otro",
};

export type Golden = {
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  primary_phone: string | null;
  phones: string[];
  primary_email: string | null;
  emails: string[];
  usernames: { network: string | null; username: string }[];
  document_type: string | null;
  document_number: string | null;
  birthdate: string | null;
  addresses: { type?: string | null; value: string; city?: string | null; country?: string | null }[];
  plates: string[];
  vins: string[];
  extra: Record<string, unknown>;
  completeness_pct: number;
  keys_count: number;
};

export type GoldenResponse = {
  golden: Golden | null;
  keys: ContactKey[];
  vehicles: Vehicle[];
  consents: Consent[];
  extractions: Extraction[];
  completeness_pct: number;
};

export type GoldenSearchHit = { contact_id: number; name: string | null; matched_on: string; value: string };

export type ContactSummary = {
  id: number;
  name: string | null;
  wa_id?: string | null;
  wa_username?: string | null;
  email?: string | null;
  created_at?: string | null;
  conversations_count?: number;
  last_interaction_at?: string | null;
  keys_count?: number;
  completeness_pct?: number;
  [k: string]: unknown;
};

export type MergeCandidate = {
  id: number;
  score: number;
  matched: { key_type: string; value_normalized: string }[];
  contact_a: ContactSummary;
  contact_b: ContactSummary;
  created_at: string;
};

export type FieldScope = "contact" | "deal" | "vehicle" | "appointment" | "flow";
export const SCOPE_LABEL: Record<FieldScope, string> = {
  contact: "Cliente",
  deal: "Oportunidad",
  vehicle: "Vehículo",
  appointment: "Cita",
  flow: "Flujo (oculto)",
};
export const SECTIONS = [
  "Identidad",
  "Contacto",
  "Vehículo",
  "Nuevos",
  "Usados",
  "Taller",
  "Repuestos/Accesorios",
  "PQR",
  "Consentimientos",
  "Marketing",
  "Técnico",
  "General",
];

export type GoldenField = {
  id: number;
  key: string;
  label: string;
  type: string;
  section: string;
  scope: FieldScope;
  pipeline: string | null;
  maps_to: string | null;
  aliases: string[];
  show_in_card: boolean;
  description?: string | null;
  archived_at?: string | null;
};
/** /api/golden/fields agrupa por sección; se aceptan ambas formas (lista plana u objeto/lista por sección). */
export type GoldenFieldsResponse =
  | GoldenField[]
  | { sections: { section: string; fields: GoldenField[] }[] }
  | Record<string, GoldenField[]>;

export function flattenFields(r: GoldenFieldsResponse | null | undefined): GoldenField[] {
  if (!r) return [];
  if (Array.isArray(r)) return r;
  if ("sections" in r && Array.isArray((r as { sections: unknown }).sections))
    return (r as { sections: { section: string; fields: GoldenField[] }[] }).sections.flatMap((s) =>
      s.fields.map((f) => ({ ...f, section: f.section ?? s.section })),
    );
  return Object.entries(r as Record<string, GoldenField[]>).flatMap(([section, fs]) =>
    Array.isArray(fs) ? fs.map((f) => ({ ...f, section: f.section ?? section })) : [],
  );
}

export type ProposalField = {
  key: string;
  label: string;
  section: string;
  scope: FieldScope;
  pipeline: string | null;
  maps_to: string | null;
  type: string;
  aliases: string[];
  show_in_card: boolean;
  source_names: string[];
};
export type Proposal = { fields: ProposalField[]; notes?: string | string[] | null };
export type ApplyResult = { created: number; updated: number; archived: number; values_migrated: number };

export type GoldenReport = {
  coverage: {
    contacts: number;
    avg_completeness: number | null;
    with_document: number;
    with_email: number;
    with_birthdate: number;
    with_address: number;
    with_vehicle: number;
    with_secondary_phone: number;
  };
  extractions_series: { day: string; documents: number; conversations: number; keys_added: number }[];
  document_types: { document_type: string | null; count: number }[];
  merge_pending: number;
};

// ---------- Anuncios ----------
export type AdPlatform = "meta" | "google_ads" | "other";
export const PLATFORM_LABEL: Record<string, string> = { meta: "Meta", google_ads: "Google Ads", other: "Otros" };
export const PLATFORM_ICON: Record<string, string> = { meta: "ⓜ", google_ads: "Ⓖ", other: "◌" };

export type AdRow = {
  ad_key: string;
  platform: AdPlatform | string;
  ad_id: string | null;
  ad_name: string | null;
  campaign_id: string | null;
  campaign_name: string | null;
  thumbnail_url: string | null;
  headline: string | null;
  status: string | null;
  spend: number;
  impressions: number;
  clicks: number;
  ctr: number | null;
  conversations: number;
  new_contacts: number;
  sales: number;
  conversions: number;
  conversion_value: number;
  cost_per_conversation: number | null;
  cost_per_new_contact: number | null;
  cost_per_sale: number | null;
  roas: number | null;
};

export type AdsReport = {
  totals: {
    spend: number;
    impressions: number;
    clicks: number;
    conversations: number;
    new_contacts: number;
    sales: number;
    conversions: number;
    conversion_value: number;
    deals_won_value: number;
    cost_per_conversation: number | null;
    cost_per_new_contact: number | null;
    cost_per_sale: number | null;
    roas: number | null;
    currency: string | null;
  };
  ads: AdRow[];
  campaigns: AdRow[];
  series: { day: string; spend: number; conversations: number; sales: number }[];
};

/** /api/ads/{platform}/{ad_id}: creativo + serie + clientes recientes (campos opcionales por tolerancia). */
export type AdDetail = {
  platform?: string;
  ad_id?: string;
  ad_name?: string | null;
  name?: string | null;
  campaign_id?: string | null;
  campaign_name?: string | null;
  ad_group_name?: string | null;
  status?: string | null;
  headline?: string | null;
  body?: string | null;
  media_type?: string | null;
  media_url?: string | null;
  thumbnail_url?: string | null;
  destination?: string | null;
  post_id?: string | null;
  source_url?: string | null;
  creative?: {
    headline?: string | null;
    body?: string | null;
    media_type?: string | null;
    media_url?: string | null;
    thumbnail_url?: string | null;
  } | null;
  totals?: Partial<AdRow> | null;
  series?: { day: string; spend?: number; conversations?: number; sales?: number }[];
  contacts?: {
    id?: number;
    contact_id?: number;
    name: string | null;
    wa_id?: string | null;
    at?: string | null;
    created_at?: string | null;
    first_source_at?: string | null;
    stage?: string | null;
  }[];
  recent_contacts?: AdDetail["contacts"];
};

// ---------- Utilidades ----------
/** PATCH/DELETE que aceptan respuesta vacía (204). */
export async function mutate<T = unknown>(path: string, method: "PATCH" | "DELETE", body?: unknown): Promise<T | true> {
  try {
    return await api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    if (e instanceof SyntaxError) return true; // cuerpo vacío
    throw e;
  }
}

/** Plataforma publicitaria a partir del canal de atribución de la fuente. */
export function platformOf(channel: string | null | undefined): AdPlatform {
  if (!channel) return "other";
  if (["meta_ctwa", "meta_ads_web", "instagram", "messenger"].includes(channel)) return "meta";
  if (channel === "google_ads") return "google_ads";
  return "other";
}

export const SOURCE_CHANNEL_ICON: Record<string, string> = {
  meta_ctwa: "ⓜ",
  meta_ads_web: "ⓜ",
  instagram: "📸",
  messenger: "💬",
  google_ads: "Ⓖ",
  organic_web: "🌐",
  paid_other: "💳",
  offline: "🏷️",
  campaign: "📣",
  webchat: "🗨️",
  direct: "↪",
};

export const money = (n: number | null | undefined, currency?: string | null, digits = 0) =>
  n == null
    ? "—"
    : `${currency ? currency + " " : "$"}${Number(n).toLocaleString("es", { maximumFractionDigits: digits })}`;

/** ¿La búsqueda parece una llave (placa, VIN, documento, correo)? Devuelve el tipo para el mensaje. */
export function looksLikeKey(q: string): string | null {
  const v = q.trim();
  if (!v) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return "correo";
  const compact = v.replace(/[\s.\-]/g, "").toUpperCase();
  if (/^[A-HJ-NPR-Z0-9]{17}$/.test(compact) && /\d/.test(compact) && /[A-Z]/.test(compact)) return "VIN";
  if (/^[A-Z]{3}\d{2}[A-Z0-9]$/.test(compact)) return "placa";
  // Documento: con separadores (1.020.345.678) o de 6 a 9 dígitos (un celular tiene 10 o más)
  if (/^\d{6,12}$/.test(compact) && (/[.\-]/.test(v) || compact.length <= 9)) return "documento";
  return null;
}

export const MATCHED_ON_LABEL: Record<string, string> = {
  plate: "placa",
  vin: "VIN",
  document: "documento",
  email: "correo",
  phone: "teléfono",
  username: "usuario",
};
