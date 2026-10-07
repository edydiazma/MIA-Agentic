"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { OnboardingState } from "@/lib/onboarding-types";

const DISMISS_KEY = "onboarding_banner_dismissed";

/** Inicio: recordatorio para terminar la configuración (solo administradores, mientras no esté completa). */
export default function OnboardingBanner() {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [hidden, setHidden] = useState(true);

  useEffect(() => {
    try {
      if (sessionStorage.getItem(DISMISS_KEY)) return;
    } catch {}
    api<OnboardingState>("/api/onboarding").then(
      (s) => {
        setState(s);
        setHidden(!!s.org.onboarding_completed_at);
      },
      () => setHidden(true),
    );
  }, []);

  if (hidden || !state) return null;
  const steps = state.steps ?? [];
  const done = steps.filter((s) => s.status === "done" || s.status === "warning" || s.status === "skipped").length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;

  return (
    <div className="ob-banner" role="region" aria-label="Configuración pendiente">
      <div className="ob-banner-icon" aria-hidden>
        🚀
      </div>
      <div className="ob-banner-text">
        <span className="strong">Termina de configurar tu cuenta</span>
        <span className="small muted">
          {pct}% listo · conecta y valida tu número, aprueba tus plantillas y activa tu WhatsApp.
        </span>
        <div className="ob-progress small" aria-hidden>
          <div className="ob-progress-bar" style={{ width: `${pct}%` }} />
        </div>
      </div>
      <Link className="button primary" href="/onboarding">
        Continuar configuración
      </Link>
      <button
        className="icon"
        aria-label="Ocultar por ahora"
        onClick={() => {
          try {
            sessionStorage.setItem(DISMISS_KEY, "1");
          } catch {}
          setHidden(true);
        }}
      >
        ✕
      </button>
    </div>
  );
}
