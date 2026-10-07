/** Operación de contact center (§18.1): estados de asesor, horarios por grupo y enrutamiento. */

export type AgentStatusDef = {
  id: number;
  key: string;
  name: string;
  icon: string | null;
  color: string | null;
  receives_conversations: boolean;
  counts_as_working: boolean;
  is_default: boolean;
  is_offline: boolean;
  position: number;
  is_active: boolean;
  is_system: boolean;
};

export type MyStatus = { status: AgentStatusDef | null; since: string | null; availability: string };

export type StatusBoardRow = {
  agent_id: number;
  name: string;
  role: string;
  employee_code: string | null;
  status: AgentStatusDef | null;
  since: string | null;
  seconds_in_status: number | null;
  online: boolean;
  open_conversations: number;
  session_started_at: string | null;
};

export type DayKey = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";
export const DAY_KEYS: DayKey[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export const DAY_NAMES: Record<DayKey, string> = {
  mon: "Lunes", tue: "Martes", wed: "Miércoles", thu: "Jueves", fri: "Viernes", sat: "Sábado", sun: "Domingo",
};
export type Range = { from: string; to: string };
export type Schedule = Partial<Record<DayKey, Range[]>>;

export type HoursRow = {
  id: number;
  group_id: number | null;
  timezone: string;
  schedule: Schedule;
  out_of_hours_message: string | null;
  assign_anyway: boolean;
  pause_bot: boolean;
  inherit_general: boolean;
  updated_at: string;
};

export type HoursOverview = {
  general: HoursRow | null;
  default_timezone: string;
  open_now: boolean;
  configured: boolean;
  groups: { group_id: number; group_name: string; hours: HoursRow | null; uses_general: boolean; open_now: boolean }[];
  holidays: { id: number; day: string; name: string; group_id: number | null }[];
};

export type Routing = "least_loaded" | "round_robin" | "sticky_owner" | "manual";
export const ROUTING_LABELS: Record<Routing, string> = {
  least_loaded: "Menor carga",
  round_robin: "Turno rotativo",
  sticky_owner: "Dueño del cliente primero",
  manual: "Manual (sin asignación automática)",
};

export type GroupSettings = {
  group_id: number;
  name: string;
  routing: Routing;
  transfer_group_ids: number[] | null;
  channel_ids: number[];
  max_open_per_agent: number | null;
  members: number[];
  supervisor_ids: number[];
};

export type RoutingSettings = {
  sticky_agent: boolean;
  owner_on_first_assignment: boolean;
  assign_when_none_available: boolean;
  session_timeout_minutes: number;
  offline_grace_seconds: number;
};

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${Math.max(0, Math.floor(seconds))} s`;
}
