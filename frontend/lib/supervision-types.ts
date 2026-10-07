import type { Conversation } from "@/lib/api";

/** Supervisión y KPIs de servicio (§18.2). */
export type MonitoringItem = Conversation & {
  wait_minutes: number | null;
  assignment_count: number;
  is_returning: boolean;
  minutes_in_bot: number | null;
};

export type MonitoringResponse = { total: number; items: MonitoringItem[] };

export type SupervisedGroups = {
  scope: "all" | "groups" | "own";
  groups: { id: number; name: string }[];
  channels: { id: number; name: string }[];
};

export type BulkAssignResult = {
  assigned: { id: number; assigned_agent_id: number | null; group_id: number | null }[];
  skipped: { id: number; reason: string }[];
};

export type ServiceKpis = {
  cases: number;
  unique_contacts: number;
  returning_cases: number;
  bot_only: number;
  handoffs: number;
  attended: number;
  not_attended: number;
  abandoned: number;
  reassigned: number;
  closed: number;
  aht_s: number | null;
  asa_s: number | null;
  attention_rate_pct: number | null;
  abandonment_rate_pct: number | null;
  bot_containment_pct: number | null;
};

export type ServiceReport = {
  start: string;
  end: string;
  totals: ServiceKpis;
  by_group: (ServiceKpis & { group_id: number | null; name: string })[];
  by_agent: (ServiceKpis & { agent_id: number | null; name: string })[];
  series: (ServiceKpis & { day: string })[];
};

export type LoginReport = {
  start: string;
  end: string;
  statuses: { key: string; name: string; color: string | null; counts_as_working: boolean }[];
  agents: {
    agent_id: number;
    name: string;
    employee_code: string | null;
    worked_s: number;
    total_s: number;
    by_status: Record<string, number>;
    sessions: { day: string; first_login: string; last_logout: string | null; sessions: number }[];
  }[];
};

export type LinksReport = {
  start: string;
  end: string;
  total: number;
  domains: ({ domain: string; total: number } & Record<string, number | string>)[];
  urls: ({ url: string; total: number } & Record<string, number | string>)[];
};

/** "1 h 05 min", "12 min", "45 s". */
export function fmtDuration(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${String(m % 60).padStart(2, "0")} min`;
}
