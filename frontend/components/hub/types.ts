// Hub de integraciones (§21.3): tiendas, calendarios, conectores propios y exportación de datos.

export type HubRun = {
  id: number;
  entity: string;
  direction: string;
  status: string;
  fetched: number;
  created: number;
  updated: number;
  skipped: number;
  failed: number;
  error: string | null;
  sample_errors: { ref?: string; error?: string }[];
  started_at: string;
  finished_at: string | null;
};

export type HubConnection = {
  id: number;
  provider: string;
  provider_label: string;
  label: string | null;
  status: string;
  last_error: string | null;
  external_account_id: string | null;
  instance_url: string | null;
  connector_id: number | null;
  sync_enabled: boolean;
  last_sync_at: string | null;
  settings: Record<string, unknown>;
  secrets_set: string[];
  webhook_url: string | null;
  orders: number | null;
  created_at: string;
  last_run: HubRun | null;
};

export type HubProvider = {
  provider: string;
  label: string;
  kind: "commerce" | "calendar" | "crm" | "custom";
  multiple: boolean;
  oauth: boolean;
  fields?: string[];
  path?: string;
};

export type ConnectorDefinition = {
  id: number;
  key: string;
  name: string;
  description: string | null;
  base_url: string;
  auth: Record<string, unknown>;
  endpoints: { key: string; method?: string; path: string; [k: string]: unknown }[];
  mappings: Record<string, unknown>;
  webhooks: Record<string, unknown>;
  version: number;
  is_published: boolean;
  template: boolean;
  updated_at: string;
};

export type ExternalOrder = {
  id: number;
  connection_id: number;
  store: string;
  provider: string;
  external_id: string;
  order_number: string | null;
  status: "pending" | "paid" | "fulfilled" | "cancelled" | "refunded";
  status_raw: string | null;
  total: number | null;
  currency: string | null;
  items: { sku?: string; name?: string; quantity?: number; price?: number }[];
  contact_id: number | null;
  attribution_id: number | null;
  placed_at: string | null;
};

export const ORDER_STATUS: Record<ExternalOrder["status"], { label: string; tone: "neutral" | "ok" | "warn" | "bad" | "info" }> = {
  pending: { label: "Pendiente", tone: "warn" },
  paid: { label: "Pagado", tone: "ok" },
  fulfilled: { label: "Entregado", tone: "info" },
  cancelled: { label: "Cancelado", tone: "bad" },
  refunded: { label: "Reembolsado", tone: "bad" },
};

export type DataExport = {
  id: number;
  name: string;
  destination: "download" | "s3" | "gcs" | "bigquery" | "sftp";
  config: Record<string, unknown> & { secrets_set?: string[] };
  datasets: string[];
  format: "parquet" | "csv" | "jsonl";
  effective_format: string;
  schedule: "hourly" | "daily" | "weekly" | "manual";
  incremental: boolean;
  include_sensitive: boolean;
  last_export_at: string | null;
  last_status: string | null;
  last_error: string | null;
  is_active: boolean;
  created_at: string;
};

export type ExportRun = {
  id: number;
  status: string;
  rows_exported: number;
  bytes: number;
  files: { dataset: string; rows: number; bytes?: number; target?: string; format?: string }[];
  watermark: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
};

export type DatasetInfo = { key: string; columns: string[]; sensitive_columns: string[]; watermark: string };
