/** Cliente 360 (docs/data-model.md §14): identidad de WhatsApp, métricas de interacción y productos. */
import { CHANNEL_ICONS, CHANNEL_LABELS, fmtDateTime, timeAgo, type ChannelProvider, type Contact } from "@/lib/api";

export type Ref = { id: number; name: string } | null;
export type CustomerChannel = { id: number; name: string; provider: ChannelProvider; display_phone: string | null };

/** Fila de la lista de clientes (los campos 360 pueden faltar si el backend aún no los envía). */
export type ContactRow = Contact & {
  wa_username?: string | null;
  wa_bsuid?: string | null;
  updated_at?: string | null;
  channel_providers?: ChannelProvider[];
  /** Canales de la empresa por los que escribió el cliente. */
  channels?: CustomerChannel[];
  last_agent?: Ref;
  last_typification?: Ref;
  first_interaction_at?: string | null;
  first_inbound_at?: string | null;
  last_interaction_at?: string | null;
  last_inbound_at?: string | null;
  last_outbound_at?: string | null;
  messages_in?: number;
  messages_out?: number;
  conversations_count?: number;
  flow_runs_count?: number;
  last_flow?: Ref;
  last_flow_at?: string | null;
  products_count?: number;
  last_product_name?: string | null;
  lifetime_days?: number | null;
  days_since_last_interaction?: number | null;
};

export type ColumnType = "text" | "date" | "number" | "tags" | "channels" | "ref";
export type ColumnDef = {
  key: string;
  label: string;
  type: ColumnType;
  sortable: boolean;
  default_visible: boolean;
  group: string;
};

/** Columnas por defecto si /api/contacts/columns no responde (mismo contrato). */
export const FALLBACK_COLUMNS: ColumnDef[] = [
  { key: "name", label: "Nombre completo", type: "text", sortable: true, default_visible: true, group: "Cliente" },
  { key: "wa_id", label: "Teléfono", type: "text", sortable: false, default_visible: true, group: "Cliente" },
  { key: "wa_username", label: "Usuario WhatsApp", type: "text", sortable: false, default_visible: true, group: "Cliente" },
  { key: "tags", label: "Etiquetas", type: "tags", sortable: false, default_visible: true, group: "Cliente" },
  { key: "channels", label: "Canales", type: "channels", sortable: false, default_visible: true, group: "Cliente" },
  { key: "last_agent", label: "Agente", type: "ref", sortable: false, default_visible: true, group: "Atención" },
  { key: "last_typification", label: "Tipificación", type: "ref", sortable: false, default_visible: true, group: "Atención" },
  { key: "created_at", label: "F. creación", type: "date", sortable: true, default_visible: true, group: "Fechas" },
  { key: "updated_at", label: "F. actualización", type: "date", sortable: true, default_visible: true, group: "Fechas" },
  { key: "email", label: "Email", type: "text", sortable: false, default_visible: false, group: "Cliente" },
  { key: "stage", label: "Etapa", type: "text", sortable: false, default_visible: false, group: "Cliente" },
  { key: "first_interaction_at", label: "Primera interacción", type: "date", sortable: true, default_visible: false, group: "Interacción" },
  { key: "last_interaction_at", label: "Última interacción", type: "date", sortable: true, default_visible: false, group: "Interacción" },
  { key: "lifetime_days", label: "Días de vida", type: "number", sortable: true, default_visible: false, group: "Interacción" },
  { key: "days_since_last_interaction", label: "Días sin interacción", type: "number", sortable: true, default_visible: false, group: "Interacción" },
  { key: "conversations_count", label: "Conversaciones", type: "number", sortable: true, default_visible: false, group: "Interacción" },
  { key: "messages_in", label: "Mensajes recibidos", type: "number", sortable: true, default_visible: false, group: "Interacción" },
  { key: "messages_out", label: "Mensajes enviados", type: "number", sortable: false, default_visible: false, group: "Interacción" },
  { key: "flow_runs_count", label: "Flujos", type: "number", sortable: false, default_visible: false, group: "Flujos" },
  { key: "last_flow", label: "Último flujo", type: "ref", sortable: false, default_visible: false, group: "Flujos" },
  { key: "products_count", label: "Productos", type: "number", sortable: true, default_visible: false, group: "Productos" },
  { key: "last_product_name", label: "Último producto", type: "text", sortable: false, default_visible: false, group: "Productos" },
];

export type ProductStage = "mentioned" | "interested" | "quoted" | "purchased" | "not_interested";
export const PRODUCT_STAGE_LABEL: Record<ProductStage, string> = {
  mentioned: "Mencionado",
  interested: "Interesado",
  quoted: "Cotizado",
  purchased: "Comprado",
  not_interested: "No le interesa",
};
export const PRODUCT_STAGE_TONE: Record<ProductStage, "neutral" | "info" | "warn" | "ok" | "bad"> = {
  mentioned: "neutral",
  interested: "info",
  quoted: "warn",
  purchased: "ok",
  not_interested: "bad",
};
export type ProductSource = "ai" | "agent" | "flow" | "api" | "whatsapp_order" | "catalog_message" | "import";
export const PRODUCT_SOURCE_LABEL: Record<ProductSource, string> = {
  ai: "IA",
  agent: "Asesor",
  flow: "Flujo",
  api: "API",
  whatsapp_order: "Pedido WhatsApp",
  catalog_message: "Catálogo",
  import: "Importado",
};

export type CatalogProduct = {
  id: number;
  sku: string | null;
  name: string;
  price: number | null;
  currency: string | null;
  image_url: string | null;
};

export type InteractionProduct = {
  id: number;
  contact_id: number;
  conversation_id: number | null;
  product: CatalogProduct | null;
  external_ref: string | null;
  name: string;
  category: string | null;
  stage: ProductStage;
  quantity: number | null;
  unit_price: number | null;
  currency: string | null;
  source: ProductSource;
  confidence: number | null;
  created_at: string;
  created_by: Ref;
};

export type CustomersReport = {
  totals: {
    contacts: number;
    new: number;
    active: number;
    returning: number;
    avg_lifetime_days: number | null;
    avg_days_between_conversations: number | null;
    median_days_since_last_interaction: number | null;
  };
  new_series: { day: string; new: number; active: number }[];
  recency_buckets: { label: string; count: number }[];
  lifetime_buckets: { label: string; count: number }[];
  by_channel: { provider: ChannelProvider; contacts: number; new: number }[];
  by_agent: { agent_id: number | null; name: string | null; contacts: number }[];
};

export type ProductsReport = {
  totals: {
    mentioned: number;
    interested: number;
    quoted: number;
    purchased: number;
    purchased_value: number;
    contacts: number;
  };
  top: {
    product_key: string;
    product_id: number | null;
    name: string;
    mentioned: number;
    interested: number;
    quoted: number;
    purchased: number;
    purchased_value: number;
    contacts: number;
    conversion_pct: number | null;
  }[];
  series: { day: string; interested: number; quoted: number; purchased: number }[];
};

/** Fecha como en Atom: "02 oct 15:05 pm". */
export function atomDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const day = String(d.getDate()).padStart(2, "0");
  const month = d.toLocaleDateString("es", { month: "short" }).replace(".", "");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${day} ${month} ${hh}:${mm} ${d.getHours() < 12 ? "am" : "pm"}`;
}

/** Fecha completa para tooltips. */
export const fullDate = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("es", { dateStyle: "full", timeStyle: "short" }) : "";

/** Canal para mostrar: número (WhatsApp) o nombre. */
export const channelText = (c: CustomerChannel) => c.display_phone || c.name;

/** "hace 3 días" con la fecha completa para el tooltip. */
export function relDate(iso: string | null | undefined): { text: string; title: string } {
  if (!iso) return { text: "—", title: "" };
  return { text: timeAgo(iso), title: fmtDateTime(iso) };
}

export const fmtDays = (n: number | null | undefined) =>
  n == null ? "—" : `${n.toLocaleString("es", { maximumFractionDigits: 1 })} d`;

export const channelLabel = (p: ChannelProvider) => `${CHANNEL_ICONS[p] ?? ""} ${CHANNEL_LABELS[p] ?? p}`;

export const fmtMoney = (n: number | null | undefined, currency?: string | null) =>
  n == null
    ? "—"
    : `${currency ? currency + " " : ""}${Number(n).toLocaleString("es", { maximumFractionDigits: 2 })}`;
