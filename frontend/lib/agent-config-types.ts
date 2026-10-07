/** Configuración avanzada del agente (§17): reglas por fuente, recuperación, etapas, tipificaciones y webhooks entrantes. */

export type SourceRuleMatch = {
  channel?: string | null;
  source_type?: "ad" | "post" | null;
  ad_id?: string | null;
  campaign_contains?: string | null;
  link_id?: number | null;
  link_slug?: string | null;
  portal?: string | null;
  utm_source?: string | null;
  text_contains?: string | null;
};
export type SourceRuleActions = {
  group_id?: number | null;
  group?: string | null;
  tags: string[];
  stage?: { pipeline: string; key: string } | null;
  skip_intent_question: boolean;
  handoff: boolean;
  note?: string | null;
};
export type SourceRule = { name: string; enabled: boolean; match: SourceRuleMatch; actions: SourceRuleActions };
export type RecoveryAttempt = { after_hours: number; message: string; use_ai: boolean };
export type SecurityAction = "close" | "block" | "handoff" | "flag";

export type AgentAdvanced = {
  timezone: string | null;
  max_words: number | null;
  ad_context_enabled: boolean;
  ad_context_prompt: string | null;
  source_rules: SourceRule[];
  cost_optimization: boolean;
  security_enabled: boolean;
  security_prompt: string | null;
  security_action: SecurityAction;
  recovery_enabled: boolean;
  recovery_attempts: RecoveryAttempt[];
  inactivity_end_hours: number | null;
  inactivity_end_typification_id: number | null;
  extract_field_ids: number[];
};

export const ADVANCED_DEFAULTS: AgentAdvanced = {
  timezone: null,
  max_words: null,
  ad_context_enabled: true,
  ad_context_prompt: null,
  source_rules: [],
  cost_optimization: true,
  security_enabled: true,
  security_prompt: null,
  security_action: "close",
  recovery_enabled: false,
  recovery_attempts: [],
  inactivity_end_hours: null,
  inactivity_end_typification_id: null,
  extract_field_ids: [],
};

export const MATCH_CHANNELS: [string, string][] = [
  ["meta_ctwa", "Anuncio de Meta (Click to WhatsApp)"],
  ["google_ads", "Google Ads"],
  ["meta_ads_web", "Meta hacia el sitio web"],
  ["organic_web", "Sitio web orgánico"],
  ["paid_other", "Pauta (otra)"],
  ["offline", "Offline (QR, SMS)"],
  ["instagram", "Instagram"],
  ["messenger", "Messenger"],
  ["webchat", "Chat web"],
  ["campaign", "Campaña de WhatsApp"],
];
export const PORTALS: [string, string][] = [
  ["any", "Cualquier portal"],
  ["mercadolibre", "MercadoLibre"],
  ["tucarro", "TuCarro"],
  ["carroya", "Carroya"],
  ["olx", "OLX"],
  ["vendetunave", "Vendetunave"],
  ["autocosmos", "Autocosmos"],
  ["facebook_marketplace", "Facebook Marketplace"],
];
export const SECURITY_ACTIONS: [SecurityAction, string][] = [
  ["close", "Terminar la conversación con un mensaje cortés"],
  ["block", "Terminar y bloquear al contacto"],
  ["handoff", "Transferir a un asesor"],
  ["flag", "Solo marcar (sigue respondiendo)"],
];

export type PipelineStage = {
  id?: number;
  pipeline?: string;
  key: string;
  name: string;
  external_name: string | null;
  ai_condition: string | null;
  position?: number;
  probability: number | null;
  is_won: boolean;
  is_lost: boolean;
  is_active: boolean;
};
export type PipelineGroup = { pipeline: string; label: string; stages: PipelineStage[] };

export type TypificationSection = "positive" | "negative" | "followup" | "neutral";
export const SECTION_LABEL: Record<TypificationSection, string> = {
  positive: "Positiva",
  negative: "Negativa",
  followup: "Seguim.",
  neutral: "Neutral",
};
export type TypificationDetailed = {
  id: number;
  name: string;
  criteria: string | null;
  is_success: boolean;
  is_active: boolean;
  section: TypificationSection | null;
  keyword: string | null;
  group_ids: number[];
  reactivate_bot_after_h: number | null;
  required_fields: string[];
};

export type HookAction = "send_template" | "send_text" | "start_flow" | "upsert_contact" | "create_deal" | "create_appointment";
export const HOOK_ACTIONS: [HookAction, string][] = [
  ["send_template", "Enviar plantilla de WhatsApp"],
  ["send_text", "Enviar texto (dentro de la ventana de 24 h)"],
  ["start_flow", "Iniciar un flujo"],
  ["upsert_contact", "Crear o actualizar el cliente"],
  ["create_deal", "Crear oportunidad"],
  ["create_appointment", "Crear cita"],
];
export type ParamType = "text" | "number" | "date" | "datetime" | "phone" | "email" | "url" | "currency";
export const PARAM_TYPES: ParamType[] = ["text", "number", "date", "datetime", "phone", "email", "url", "currency"];
export type HookParam = {
  name: string;
  label?: string | null;
  type: ParamType;
  required: boolean;
  example?: string | number | null;
  maps_to?: string | null;
};
export type HookOptions = {
  assign_group_id?: number | null;
  assign_agent_id?: number | null;
  bot?: "on" | "off" | "keep";
  tags?: string[];
  typification?: string | null;
  pipeline?: string | null;
  dedupe_minutes?: number | null;
  prefer_free_form?: boolean;
};
export type InboundWebhook = {
  id: number;
  url: string;
  name: string;
  slug: string;
  status: "draft" | "active" | "paused";
  action: HookAction;
  channel_id: number | null;
  template_name: string | null;
  template_language: string | null;
  flow_id: number | null;
  params: HookParam[];
  params_count: number;
  options: HookOptions;
  version: number;
  executions: number;
  succeeded: number;
  failed: number;
  last_run_at: string | null;
  created_by: { id: number; name: string } | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
  token: string | null;
};
export type HookRun = {
  id: number;
  status: "succeeded" | "failed" | "rejected" | "duplicate";
  http_status: number;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  contact_id: number | null;
  conversation_id: number | null;
  error: string | null;
  latency_ms: number | null;
  ip: string | null;
  created_at: string;
};

/** Destinos de un parámetro (maps_to), agrupados para el selector. */
export const MAPS_GROUPS: { label: string; options: [string, string][] }[] = [
  { label: "Destinatario", options: [["recipient.phone", "Teléfono del cliente"], ["recipient.bsuid", "BSUID de WhatsApp"]] },
  {
    label: "Cliente",
    options: [["contact.name", "Nombre"], ["contact.email", "Correo"], ["key:document", "Documento (llave maestra)"],
      ["key:birthdate", "Fecha de nacimiento (llave maestra)"], ["key:address", "Dirección (llave maestra)"]],
  },
  {
    label: "Vehículo",
    options: [["vehicle.plate", "Placa"], ["vehicle.vin", "VIN"], ["vehicle.make", "Marca"], ["vehicle.model", "Modelo"],
      ["vehicle.year", "Año"], ["vehicle.mileage_km", "Kilometraje"], ["vehicle.insurance_due", "Vence SOAT"],
      ["vehicle.next_service_at", "Próximo mantenimiento"]],
  },
  {
    label: "Oportunidad",
    options: [["deal.name", "Nombre"], ["deal.pipeline", "Línea de negocio"], ["deal.stage", "Etapa"],
      ["deal.amount", "Monto"], ["deal.currency", "Moneda"]],
  },
  {
    label: "Cita",
    options: [["appointment.starts_at", "Fecha y hora (ISO)"], ["appointment.date", "Fecha"], ["appointment.time", "Hora"],
      ["appointment.title", "Título"], ["appointment.notes", "Notas"]],
  },
  { label: "Mensaje", options: [["message.text", "Texto del mensaje"]] },
];
