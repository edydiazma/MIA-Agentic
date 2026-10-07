/** Calidad (QA), coaching y pruebas de agentes. Espejo de backend/app/routers/{quality,agent_tests}.py */
import { api } from "@/lib/api";

export type Criterion = { key: string; label: string; description?: string; weight: number; critical: boolean };
export type Scorecard = {
  id: number;
  name: string;
  applies_to: "agent" | "bot" | "any";
  criteria: Criterion[];
  auto_review: boolean;
  sample_pct: number;
  min_messages: number;
  group_ids: number[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
};
export type ScorecardIn = Omit<Scorecard, "id" | "created_at" | "updated_at">;

export type ReviewScore = Criterion & { score: number | null; applies: boolean; evidence: string; comment: string };
export type Sentiment = "positive" | "neutral" | "negative" | "mixed";
export type Review = {
  id: number;
  conversation_id: number;
  contact_name: string | null;
  scorecard_id: number | null;
  scorecard_name: string;
  reviewer_type: "ai" | "human";
  reviewer_name: string | null;
  subject_type: "agent" | "bot";
  agent_id: number | null;
  agent_name: string | null;
  ai_agent_id: number | null;
  ai_agent_name: string | null;
  status: "pending" | "done" | "failed" | "disputed";
  scores: ReviewScore[];
  total_score: number | null;
  critical_failed: boolean;
  sentiment: Sentiment | null;
  sentiment_score: number | null;
  customer_effort: number | null;
  summary: string | null;
  error: string | null;
  dispute_note: string | null;
  created_at: string;
  updated_at: string;
  coaching?: CoachingItem[];
};
export type ReviewList = { total: number; items: Review[] };
export type ConversationQualityData = {
  conversation_id: number;
  qa_score: number | null;
  sentiment: string | null;
  reviews: Review[];
};

export type CoachingItem = {
  id: number;
  agent_id: number;
  review_id: number | null;
  criterion_key: string | null;
  title: string;
  suggestion: string;
  example: string | null;
  status: "open" | "acknowledged" | "done" | "dismissed";
  created_at: string;
  resolved_at: string | null;
};
export type CoachingCriterion = {
  key: string;
  label: string;
  avg: number | null;
  samples: number;
  prev_avg: number | null;
  trend: number | null;
  examples: { review_id: number; conversation_id: number; score: number; evidence: string | null; comment: string | null }[];
};
export type CoachingProfile = {
  agent_id: number;
  agent_name: string;
  days: number;
  reviews: number;
  avg_score: number | null;
  prev_avg_score: number | null;
  trend: number | null;
  critical_failed: number;
  criteria: CoachingCriterion[];
  weaknesses: CoachingCriterion[];
  open_items: CoachingItem[];
};

export type QASummary = {
  reviews: number;
  avg_score: number | null;
  critical_failed: number;
  critical_pct: number | null;
  positive: number;
  neutral: number;
  negative: number;
  negative_pct: number | null;
};
export type QAReport = {
  totals: QASummary & { open_coaching: number; disputed: number };
  agents: (QASummary & { agent_id: number; name: string })[];
  bot: QASummary;
  series: { day: string; reviews: number; avg_score: number | null; positive: number; neutral: number; negative: number }[];
};

export type TestTurn = { role: "user" | "assistant"; text: string };
export type TestExpectations = {
  must_include?: string[];
  must_not_include?: string[];
  expect_handoff?: boolean;
  expect_tool?: string;
  rubric?: string;
};
export type TestRun = {
  id: number;
  suite_id: number;
  ai_agent_id: number;
  config_revision_id: number | null;
  trigger: "manual" | "on_change" | "schedule";
  status: "running" | "passed" | "failed" | "error";
  total: number;
  passed: number;
  pass_pct: number | null;
  cost_usd: number;
  started_at: string;
  finished_at: string | null;
};
export type TestSuite = {
  id: number;
  ai_agent_id: number;
  name: string;
  description: string | null;
  run_on_change: boolean;
  min_pass_pct: number;
  cases: number | null;
  last_run: TestRun | null;
};
export type TestCase = {
  id: number;
  suite_id: number;
  name: string;
  turns: TestTurn[];
  expectations: TestExpectations;
  source_conversation_id: number | null;
  position: number;
};
export type TestResult = {
  id: number;
  case_id: number | null;
  case_name: string | null;
  passed: boolean;
  reply: string | null;
  checks: { check: string; passed: boolean; detail: string }[];
  judge_score: number | null;
  latency_ms: number | null;
};
export type TestRunDetail = TestRun & { results: TestResult[] };

export const SENTIMENT: Record<Sentiment, string> = {
  positive: "😊 Positivo",
  neutral: "😐 Neutral",
  negative: "😟 Negativo",
  mixed: "😕 Mixto",
};
export const REVIEW_STATUS: Record<Review["status"], [string, "ok" | "warn" | "bad" | "neutral"]> = {
  done: ["Revisada", "ok"],
  pending: ["Pendiente", "neutral"],
  failed: ["Falló", "bad"],
  disputed: ["Impugnada", "warn"],
};
export const COACHING_STATUS: Record<CoachingItem["status"], string> = {
  open: "Pendiente",
  acknowledged: "Vista",
  done: "Aplicada",
  dismissed: "Descartada",
};
export const CHECK_LABEL: Record<string, string> = {
  must_include: "Debe incluir",
  must_not_include: "No debe incluir",
  expect_handoff: "Transferencia",
  expect_tool: "Herramienta",
  rubric: "Rúbrica (juez IA)",
  responded: "Respondió",
  error: "Error",
  turns: "Mensajes",
};

export const scoreTone = (n: number | null | undefined): "ok" | "warn" | "bad" | "neutral" =>
  n == null ? "neutral" : n >= 80 ? "ok" : n >= 60 ? "warn" : "bad";
export const fmtScore = (n: number | null | undefined) => (n == null ? "—" : Math.round(n).toString());

/** PATCH con JSON (send() solo cubre POST/PUT/DELETE). */
export const patch = <T,>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

export const isSupervisor = (role: string | undefined | null) => role === "admin" || role === "supervisor";
