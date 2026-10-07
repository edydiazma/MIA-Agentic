"use client";

import { createContext, useContext } from "react";
import { API_URL, getToken } from "@/lib/api";
import { CHECK_HELP, type Answers, type CheckStatus, type HealthCheck, type OnboardingState, type StepKey, type StepStatus } from "@/lib/onboarding-types";

export type WizardCtx = {
  state: OnboardingState;
  reload: () => Promise<void>;
  answers: Answers;
  /** Cambia las respuestas (se guardan solas tras una pausa). */
  update: (patch: (a: Answers) => Answers) => void;
  /** Guarda ya las respuestas pendientes. */
  flush: () => Promise<void>;
  industry: string | null;
  setIndustry: (v: string) => void;
  goTo: (key: StepKey) => void;
  next: () => void;
};

const Ctx = createContext<WizardCtx | null>(null);
export const WizardProvider = Ctx.Provider;
export function useWizard(): WizardCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWizard fuera del asistente");
  return v;
}

/** Petición que conserva el cuerpo del error (p. ej. 409 con `blocking`). */
export async function rawRequest<T>(path: string, method: string, body?: unknown): Promise<{ ok: boolean; status: number; data: T | any }> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${API_URL}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {}
  return { ok: res.ok, status: res.status, data };
}

export function errorText(data: unknown, fallback = "No se pudo completar la acción"): string {
  if (!data || typeof data !== "object") return fallback;
  const d = (data as { detail?: unknown }).detail;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && typeof (d as { message?: unknown }).message === "string") return (d as { message: string }).message;
  return fallback;
}

const STEP_ICON: Record<StepStatus, string> = {
  pending: "",
  running: "…",
  done: "✓",
  warning: "!",
  failed: "✕",
  skipped: "–",
};
const STEP_STATUS_LABEL: Record<StepStatus, string> = {
  pending: "Pendiente",
  running: "En curso",
  done: "Listo",
  warning: "Con observaciones",
  failed: "Con error",
  skipped: "Omitido",
};

export function StepDot({ status, index }: { status: StepStatus; index: number }) {
  return (
    <span className={`ob-dot ${status}`} aria-hidden>
      {STEP_ICON[status] || index + 1}
    </span>
  );
}
export const stepStatusLabel = (s: StepStatus) => STEP_STATUS_LABEL[s];

const CHECK_ICON: Record<CheckStatus, string> = { pass: "✓", warn: "!", fail: "✕", pending: "…", skipped: "–" };
const CHECK_LABEL: Record<CheckStatus, string> = {
  pass: "Correcto",
  warn: "Atención",
  fail: "Falla",
  pending: "Pendiente",
  skipped: "No aplica",
};

/** Lista de validaciones del número, con explicación y botón «Arreglar». */
export function ChecksList({
  checks,
  onFix,
  fixing,
  compact,
}: {
  checks: HealthCheck[];
  onFix?: (key: string) => void;
  fixing?: string | null;
  compact?: boolean;
}) {
  return (
    <ul className={`ob-checks ${compact ? "compact" : ""}`}>
      {checks.map((c) => {
        const help = CHECK_HELP[c.check_key];
        return (
          <li key={c.check_key} className={`ob-check ${c.status}`}>
            <span className={`ob-check-icon ${c.status}`} aria-label={CHECK_LABEL[c.status]} title={CHECK_LABEL[c.status]}>
              {CHECK_ICON[c.status]}
            </span>
            <div className="ob-check-body">
              <div className="strong">{c.label || help?.label || c.check_key}</div>
              {c.detail && <div className="small">{c.detail}</div>}
              {!compact && help && <div className="small muted">{help.help}</div>}
            </div>
            {onFix && c.fixable && c.status !== "pass" && (
              <button className="small" onClick={() => onFix(c.check_key)} disabled={fixing === c.check_key}>
                {fixing === c.check_key ? "Arreglando…" : "Arreglar"}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Pie de cada paso: volver / continuar. */
export function StepFooter({
  onBack,
  onNext,
  nextLabel = "Continuar",
  nextDisabled,
  busy,
  extra,
}: {
  onBack?: () => void;
  onNext?: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  busy?: boolean;
  extra?: React.ReactNode;
}) {
  return (
    <div className="ob-footer">
      <div>{onBack && <button onClick={onBack}>← Atrás</button>}</div>
      <div className="inline">
        {extra}
        {onNext && (
          <button className="primary" onClick={onNext} disabled={nextDisabled || busy}>
            {busy ? "Guardando…" : nextLabel}
          </button>
        )}
      </div>
    </div>
  );
}

/** Reemplaza {{1}} / {{nombre}} por los ejemplos y los resalta. */
export function renderTemplateText(text: string, examples: string[] | Record<string, string>): React.ReactNode[] {
  const parts = text.split(/(\{\{\s*[\w.]+\s*\}\})/g);
  return parts.map((p, i) => {
    const m = p.match(/^\{\{\s*([\w.]+)\s*\}\}$/);
    if (!m) return <span key={i}>{p}</span>;
    const key = m[1];
    const value = Array.isArray(examples) ? examples[Number(key) - 1] : examples?.[key];
    return (
      <mark key={i} className="ob-var" title={`Variable {{${key}}}`}>
        {value || `{{${key}}}`}
      </mark>
    );
  });
}
