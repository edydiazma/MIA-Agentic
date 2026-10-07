/** Diagnóstico de integraciones (backend: app/routers/preflight.py, docs/data-model.md §20). */
export type CheckStatus = "pass" | "warn" | "fail" | "skipped";

export type CheckResult = {
  check_key: string;
  label: string;
  status: CheckStatus;
  detail: string | null;
  data: Record<string, unknown>;
  latency_ms: number | null;
  checked_at: string;
};

export type CheckArea = { area: string; label: string; results: CheckResult[] };

export type CheckRun = {
  id: number;
  trigger: string;
  app_version: string | null;
  passed: number;
  warned: number;
  failed: number;
  skipped: number;
  started_at: string;
  finished_at: string | null;
  running: boolean;
};

export type PreflightView = { run: CheckRun | null; areas: CheckArea[] };

export type OrgPreflight = PreflightView & {
  platform: PreflightView | null;
  status?: "done" | "running";
  run_id?: number;
};

export const STATUS_META: Record<CheckStatus, { icon: string; label: string; tone: "ok" | "warn" | "bad" | "neutral" }> = {
  pass: { icon: "✓", label: "Correcto", tone: "ok" },
  warn: { icon: "!", label: "Advertencia", tone: "warn" },
  fail: { icon: "✕", label: "Falla", tone: "bad" },
  skipped: { icon: "–", label: "Omitido", tone: "neutral" },
};

export const ALL_AREAS = [
  "infra", "supabase", "meta", "google_ads", "hubspot", "salesforce", "stripe", "email", "sso", "push", "voice", "ai",
] as const;
