"use client";

import { useState } from "react";
import Link from "next/link";
import { STEP_FALLBACK, STEP_ORDER, type StepKey } from "@/lib/onboarding-types";
import { ErrorBox } from "@/components/ui";
import { ChecksList, StepDot, StepFooter, errorText, rawRequest, stepStatusLabel, useWizard } from "./common";

export default function GoLiveStep() {
  const { state, reload, goTo } = useWizard();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blocking, setBlocking] = useState<string[]>([]);
  const completed = state.run?.status === "completed" || !!state.org.onboarding_completed_at;

  async function goLive() {
    setBusy(true);
    setError(null);
    setBlocking([]);
    const r = await rawRequest<{ completed_at: string }>("/api/onboarding/go-live", "POST");
    setBusy(false);
    if (r.ok) {
      await reload();
      return;
    }
    setError(errorText(r.data, "No se pudo activar la cuenta"));
    const b = (r.data?.blocking ?? r.data?.detail?.blocking) as string[] | undefined;
    if (Array.isArray(b)) setBlocking(b);
  }

  if (completed)
    return (
      <div className="ob-step">
        <section className="ob-done" aria-live="polite">
          <div className="ob-confetti" aria-hidden>
            {Array.from({ length: 18 }).map((_, i) => (
              <i key={i} style={{ ["--i" as string]: i }} />
            ))}
          </div>
          <div className="ob-success-icon big" aria-hidden>
            ✓
          </div>
          <h2>¡Tu WhatsApp está en vivo!</h2>
          <p className="muted">El bot ya responde, los flujos están activos y tu equipo puede atender desde la bandeja.</p>
          <div className="ob-next-actions">
            <Link className="ob-action" href="/conversaciones">
              <span aria-hidden>💬</span>
              <span className="strong">Ir a la bandeja</span>
              <span className="small muted">Atiende las conversaciones en tiempo real</span>
            </Link>
            <Link className="ob-action" href="/campanas">
              <span aria-hidden>📣</span>
              <span className="strong">Crear una campaña</span>
              <span className="small muted">Con tus plantillas aprobadas</span>
            </Link>
            <Link className="ob-action" href="/automatizaciones/flujos">
              <span aria-hidden>🧩</span>
              <span className="strong">Diseñar un flujo</span>
              <span className="small muted">Menús, calificación y agendamiento</span>
            </Link>
            <Link className="ob-action" href="/configuraciones/mensajes-disparadores">
              <span aria-hidden>🔗</span>
              <span className="strong">Enlaces con tracking</span>
              <span className="small muted">Mide qué anuncio trae cada chat</span>
            </Link>
          </div>
        </section>
      </div>
    );

  const steps = STEP_ORDER.filter((k) => k !== "go_live").map((k) => state.steps.find((s) => s.key === k) ?? { key: k, status: "pending" as const, label: STEP_FALLBACK[k].label, required: false, error: null });

  return (
    <div className="ob-step">
      <section className="card">
        <h3>Revisión final</h3>
        <ul className="ob-final-list">
          {steps.map((s, i) => {
            const blocked = blocking.includes(s.key);
            return (
              <li key={s.key} className={blocked ? "blocked" : ""}>
                <StepDot status={s.status} index={i} />
                <span className="strong">{s.label || STEP_FALLBACK[s.key as StepKey].label}</span>
                <span className="small muted">
                  {stepStatusLabel(s.status)}
                  {s.required ? "" : " · opcional"}
                </span>
                {(blocked || (s.required && s.status !== "done" && s.status !== "warning")) && (
                  <button className="link small" onClick={() => goTo(s.key as StepKey)}>
                    Ir al paso
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </section>
      {state.checks.length > 0 && (
        <section className="card">
          <h3>Estado del número</h3>
          <ChecksList checks={state.checks} compact />
        </section>
      )}
      <ErrorBox error={error} />
      {blocking.filter((b) => !STEP_ORDER.includes(b as StepKey)).length > 0 && (
        <p className="small ob-note warn">Falta resolver: {blocking.filter((b) => !STEP_ORDER.includes(b as StepKey)).join(", ")}.</p>
      )}
      <StepFooter onBack={() => goTo("test")} onNext={goLive} busy={busy} nextLabel="🚀 Activar mi WhatsApp" />
    </div>
  );
}
