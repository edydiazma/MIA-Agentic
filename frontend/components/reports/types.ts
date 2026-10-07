/** Formas de respuesta de backend/app/routers/reports.py */

export type RealtimeReport = {
  updated_at: string;
  by_status: { bot: number; human: number };
  waiting_unassigned: number;
  oldest_wait_minutes: number;
  agents: {
    id: number;
    name: string;
    online: boolean;
    availability: "available" | "away" | "busy";
    groups: (string | null)[];
    open: number;
    unread: number;
    /** Estado personalizado y tiempo en él (§18.1); null sin estado asignado. */
    status?: { key: string; name: string; color: string | null; icon: string | null;
               receives_conversations: boolean; seconds: number | null } | null;
  }[];
  by_group: Record<string, number>;
  avg_wait_minutes_now?: number;
  /** Contadores desde las 00:00 (zona de la empresa). */
  today?: { incoming: number; handoffs: number; closed: number; attended: number; abandoned: number;
            avg_first_response_s: number | null };
  groups?: { group_id: number; name: string; agents: number; online: number; queue: number; open: number;
             longest_wait_minutes: number }[];
};

export type GeneralReport = {
  range: { start: string; end: string };
  totals: {
    new_conversations: number;
    inbound_messages: number;
    bot_messages: number;
    agent_messages: number;
    campaign_messages: number;
    handoffs: number;
    closed: number;
    bot_resolution_pct: number | null;
    first_response_median_min: number | null;
    sla_minutes: number;
    sla_pct: number | null;
  };
  typifications: Record<string, number>;
  series: { day: string; inbound: number; bot: number; agent: number; campaign: number; new_conversations: number; handoffs: number }[];
};

export type StagesReport = Partial<Record<"lead" | "prospect" | "client" | "lost", number>>;

export type InboundReport = {
  messages: number;
  by_type: Record<string, number>;
  by_hour: number[];
  conversations: number;
  by_source: { ads: number; organic: number };
  bots: {
    messages: number;
    conversations: number;
    handoffs: number;
    handoff_pct: number | null;
    top_handoff_reasons: [string, number][];
  };
};

export type CtwaReport = {
  total: number;
  ads: {
    ad_id: string | null;
    headline: string | null;
    source_type: string | null;
    url: string | null;
    conversations: number;
    handoffs: number;
    sales: number;
  }[];
};

export type OutboundReport = {
  total: number;
  by_sender: Record<string, number>;
  by_status: Record<string, number>;
  individual_templates: ({ template: string; total?: number } & Record<string, number | string>)[];
  campaigns: {
    id: number;
    name: string;
    template: string;
    status: string;
    total?: number;
    sent?: number;
    delivered?: number;
    read?: number;
    failed?: number;
    not_delivered?: number;
  }[];
};

export type AgentsReport = {
  sla_minutes: number;
  sla_pct: number | null;
  handoffs_answered: number;
  handoffs: number;
  agents: {
    id: number;
    name: string;
    online: boolean;
    availability: "available" | "away" | "busy";
    is_active: boolean;
    messages_sent: number;
    conversations_closed: number;
    first_response_median_min: number | null;
    sla_pct: number | null;
  }[];
};

export type BillingReport = {
  categories: Record<string, { total?: number; billable?: number }>;
  billable_total: number;
  series: ({ day: string } & Record<string, number | string>)[];
  note: string;
};

export const MSG_TYPE_LABEL: Record<string, string> = {
  text: "Texto",
  image: "Imagen",
  audio: "Nota de voz",
  video: "Video",
  document: "Documento",
  sticker: "Sticker",
  location: "Ubicación",
  interactive: "Botón / lista",
  contacts: "Contacto",
  unsupported: "No soportado",
};

export const PRICING_LABEL: Record<string, string> = {
  marketing: "Marketing",
  utility: "Utilidad",
  authentication: "Autenticación",
  authentication_international: "Autenticación internacional",
  service: "Servicio",
  referral_conversion: "Conversión desde anuncio (gratis)",
};

export const SENDER_LABEL: Record<string, string> = { bot: "Bot", agent: "Asesor", campaign: "Campaña" };
export const MSG_STATUS_LABEL: Record<string, string> = {
  pending: "Pendiente",
  sent: "Enviado",
  delivered: "Entregado",
  read: "Leído",
  failed: "Fallido",
};

/** Porcentaje a 1 decimal o null si el denominador es 0. */
export const pct = (a: number | undefined, b: number | undefined) =>
  b ? Math.round((1000 * (a ?? 0)) / b) / 10 : null;
