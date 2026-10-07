import type { Agent } from "@/lib/api";

export type OrgStatus = "trial" | "active" | "past_due" | "suspended" | "cancelled";

export const ORG_STATUS_LABEL: Record<OrgStatus, string> = {
  trial: "Prueba",
  active: "Activa",
  past_due: "Pago pendiente",
  suspended: "Suspendida",
  cancelled: "Cancelada",
};

export const ORG_STATUS_TONE: Record<OrgStatus, "ok" | "warn" | "bad" | "info"> = {
  trial: "info",
  active: "ok",
  past_due: "warn",
  suspended: "bad",
  cancelled: "bad",
};

export type OrgSummary = { id: number; name: string; status: OrgStatus };

export type LoginResponse =
  | { choose_org: OrgSummary[] }
  | { access_token: string; agent: Agent; organization: OrgSummary };

export type PlanLimits = Record<string, number | null>;
export type PlanFeatures = Record<string, boolean>;

export type PublicPlan = {
  key: string;
  name: string;
  description?: string | null;
  price_month_usd: number;
  limits: PlanLimits;
  features: PlanFeatures;
  purchasable?: boolean;
};

export type Usage = { used: number; limit: number | null; allowed: boolean };

export type PlanStatus = {
  organization: {
    id: number;
    name: string;
    status: OrgStatus;
    trial_ends_at: string | null;
    trial_days_left: number | null;
    country: string | null;
  };
  plan: (PublicPlan & { id: number }) | null;
  subscription: {
    provider: string;
    status: string;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    has_customer: boolean;
  } | null;
  usage: Record<string, Usage>;
  consumption: { messages_out: number; ai_calls: number; ai_cost_usd: number; ai_tokens: number; period_start: string };
  labels: { metrics: Record<string, string>; features: Record<string, string> };
};

export type SignupConfig = { enabled: boolean; trial_days: number; default_plan: string; plans: PublicPlan[] };

export type EmbeddedSignupConfig = { app_id: string | null; config_id: string | null; api_version: string; enabled: boolean };

// ---------- Back-office de la plataforma ----------
export type PlatformPlan = PublicPlan & {
  id: number;
  provider_price_id: string | null;
  is_public: boolean;
  position: number;
};

export type PlatformOrg = {
  id: number;
  name: string;
  slug: string;
  status: OrgStatus;
  country: string | null;
  plan: PlatformPlan | null;
  trial_days_left: number | null;
  trial_ends_at: string | null;
  created_at: string;
  usage: Record<string, number>;
  users: number;
  channels: number;
  subscription: { provider: string; status: string; current_period_end: string | null } | null;
};

export type PlatformOrgDetail = PlanStatus & { admins: { name: string; email: string }[]; conversations_total: number };

export type PlatformMetrics = {
  organizations: number;
  by_status: Partial<Record<OrgStatus, number>>;
  by_plan: Record<string, number>;
  mrr_usd: number;
  conversations_month: number;
};

export const fmtLimit = (n: number | null | undefined) => (n == null ? "Ilimitado" : n.toLocaleString("es"));
