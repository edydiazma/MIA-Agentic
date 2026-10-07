"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, send } from "@/lib/api";
import { ErrorBox, Loading } from "@/components/ui";
import {
  NEEDS_CHANNEL,
  ONBOARDING_SKIP_KEY,
  STEP_FALLBACK,
  STEP_ORDER,
  type Answers,
  type OnboardingRun,
  type OnboardingState,
  type OnboardingStep,
  type StepKey,
} from "@/lib/onboarding-types";
import { StepDot, WizardProvider, stepStatusLabel, type WizardCtx } from "@/components/onboarding/common";
import CompanyStep from "@/components/onboarding/CompanyStep";
import WhatsAppStep from "@/components/onboarding/WhatsAppStep";
import ValidateStep from "@/components/onboarding/ValidateStep";
import ProfileStep from "@/components/onboarding/ProfileStep";
import TemplatesStep from "@/components/onboarding/TemplatesStep";
import AIStep from "@/components/onboarding/AIStep";
import TeamStep from "@/components/onboarding/TeamStep";
import TestStep from "@/components/onboarding/TestStep";
import GoLiveStep from "@/components/onboarding/GoLiveStep";

const STEP_VIEW: Record<StepKey, () => React.JSX.Element> = {
  company: CompanyStep,
  whatsapp: WhatsAppStep,
  validate: ValidateStep,
  profile: ProfileStep,
  templates: TemplatesStep,
  ai: AIStep,
  team: TeamStep,
  test: TestStep,
  go_live: GoLiveStep,
};

const SAVE_DELAY = 800;

export default function OnboardingPage() {
  const router = useRouter();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Answers>({});
  const [industry, setIndustryState] = useState<string | null>(null);
  const [current, setCurrent] = useState<StepKey>("company");
  const [saving, setSaving] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const dirty = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latest = useRef<{ answers: Answers; industry: string | null; current: StepKey }>({ answers: {}, industry: null, current: "company" });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const initialized = useRef(false);

  const load = useCallback(async () => {
    try {
      let s = await api<OnboardingState>("/api/onboarding");
      if (!s.run) {
        await send<OnboardingRun>("/api/onboarding/restart", "POST");
        s = await api<OnboardingState>("/api/onboarding");
      }
      setState(s);
      setLoadError(null);
      if (!initialized.current && s.run) {
        initialized.current = true;
        setAnswers(s.run.answers ?? {});
        setIndustryState(s.run.industry ?? s.org.industry);
        setCurrent(STEP_ORDER.includes(s.run.current_step) ? s.run.current_step : "company");
      }
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    latest.current = { answers, industry, current };
  }, [answers, industry, current]);

  const flush = useCallback(async () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    if (!dirty.current) return;
    dirty.current = false;
    setSaving("saving");
    try {
      const { answers: a, industry: ind, current: cur } = latest.current;
      await send("/api/onboarding/answers", "PUT", { industry: ind, answers: a, current_step: cur });
      setSaving("saved");
    } catch {
      dirty.current = true;
      setSaving("error");
    }
  }, []);

  const schedule = useCallback(() => {
    dirty.current = true;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), SAVE_DELAY);
  }, [flush]);

  // Guarda lo pendiente al cerrar la pestaña
  useEffect(() => {
    const onUnload = () => {
      if (dirty.current) void flush();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [flush]);

  const update = useCallback(
    (patch: (a: Answers) => Answers) => {
      setAnswers((a) => {
        const n = patch(a);
        latest.current = { ...latest.current, answers: n };
        return n;
      });
      schedule();
    },
    [schedule],
  );

  const setIndustry = useCallback(
    (v: string) => {
      setIndustryState(v);
      latest.current = { ...latest.current, industry: v };
      schedule();
    },
    [schedule],
  );

  const goTo = useCallback(
    (key: StepKey) => {
      setCurrent(key);
      latest.current = { ...latest.current, current: key };
      schedule();
      window.scrollTo({ top: 0, behavior: "smooth" });
      setTimeout(() => headingRef.current?.focus(), 50);
    },
    [schedule],
  );

  const steps: OnboardingStep[] = useMemo(
    () =>
      STEP_ORDER.map((key) => {
        const s = state?.steps.find((x) => x.key === key);
        return (
          s ?? {
            key,
            label: STEP_FALLBACK[key].label,
            description: STEP_FALLBACK[key].description,
            status: "pending",
            attempts: 0,
            result: {},
            error: null,
            required: ["company", "whatsapp", "validate", "templates", "go_live"].includes(key),
          }
        );
      }),
    [state],
  );

  const next = useCallback(() => {
    const i = STEP_ORDER.indexOf(latest.current.current);
    if (i < STEP_ORDER.length - 1) goTo(STEP_ORDER[i + 1]);
  }, [goTo]);

  async function saveAndExit() {
    await flush();
    try {
      sessionStorage.setItem(ONBOARDING_SKIP_KEY, "1");
    } catch {}
    router.push("/");
  }

  if (!state) return loadError ? <div className="ob-shell"><div className="ob-main"><ErrorBox error={loadError} /><button onClick={() => void load()}>Reintentar</button></div></div> : <div className="ob-shell"><Loading /></div>;

  const hasChannel = !!state.run?.channel_id;
  const doneCount = steps.filter((s) => s.status === "done" || s.status === "warning" || s.status === "skipped").length;
  const pct = Math.round((doneCount / steps.length) * 100);
  const View = STEP_VIEW[current];
  const meta = steps.find((s) => s.key === current)!;
  const locked = NEEDS_CHANNEL.includes(current) && !hasChannel;

  const ctx: WizardCtx = { state, reload: load, answers, update, flush, industry, setIndustry, goTo, next };

  return (
    <WizardProvider value={ctx}>
      <div className="ob-shell">
        <header className="ob-top">
          <div className="ob-brand">
            <span className="brand-mark">◆</span> Configura tu cuenta
          </div>
          <div className="ob-progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Progreso de la configuración">
            <div className="ob-progress-bar" style={{ width: `${pct}%` }} />
          </div>
          <span className="small muted ob-progress-label">{pct}% completado</span>
          <span className="small muted ob-saving" aria-live="polite">
            {saving === "saving" ? "Guardando…" : saving === "saved" ? "Guardado" : saving === "error" ? "No se pudo guardar" : ""}
          </span>
          <button onClick={saveAndExit}>Guardar y salir</button>
        </header>
        <div className="ob-body">
          <nav className="ob-stepper" aria-label="Pasos de la configuración">
            <ol>
              {steps.map((s, i) => {
                const disabled = NEEDS_CHANNEL.includes(s.key) && !hasChannel;
                return (
                  <li key={s.key}>
                    <button
                      className={`ob-stepper-item ${s.key === current ? "current" : ""} ${s.status}`}
                      aria-current={s.key === current ? "step" : undefined}
                      onClick={() => goTo(s.key)}
                      title={disabled ? "Primero conecta tu número de WhatsApp" : undefined}
                    >
                      <StepDot status={s.status} index={i} />
                      <span className="ob-stepper-text">
                        <span className="ob-stepper-label">
                          {s.label || STEP_FALLBACK[s.key].label}
                          {!s.required && <span className="small muted"> · opcional</span>}
                        </span>
                        <span className="small muted">{s.status === "pending" ? STEP_FALLBACK[s.key].description : stepStatusLabel(s.status)}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
          <main className="ob-main">
            <div className="ob-step-head">
              <span className="small muted">
                Paso {STEP_ORDER.indexOf(current) + 1} de {STEP_ORDER.length}
              </span>
              <h1 ref={headingRef} tabIndex={-1}>
                {meta.label || STEP_FALLBACK[current].label}
              </h1>
              <p className="muted">{meta.description || STEP_FALLBACK[current].description}</p>
              {meta.status === "failed" && meta.error && <ErrorBox error={meta.error} />}
            </div>
            {locked ? (
              <section className="ob-hero-card">
                <h3>Primero conecta tu número</h3>
                <p className="small muted">Este paso usa tu número de WhatsApp. Conéctalo y volvemos aquí automáticamente.</p>
                <button className="primary" onClick={() => goTo("whatsapp")}>
                  Ir a «Conectar WhatsApp»
                </button>
              </section>
            ) : (
              <View key={current} />
            )}
          </main>
        </div>
      </div>
    </WizardProvider>
  );
}
