"use client";

import Link from "next/link";
import type { PlanStatus } from "@/lib/saas-types";

/** Aviso global de prueba por vencer, pago pendiente, cancelación o suspensión. */
export function PlanBanner({ plan }: { plan: PlanStatus | null }) {
  if (!plan) return null;
  const { status, trial_days_left } = plan.organization;
  let tone: "warn" | "bad" = "warn";
  let text: string | null = null;
  if (status === "trial" && trial_days_left != null) {
    text =
      trial_days_left > 0
        ? `Estás en período de prueba: ${trial_days_left} ${trial_days_left === 1 ? "día restante" : "días restantes"}.`
        : "Tu período de prueba terminó. Elige un plan para seguir usando la plataforma.";
    if (trial_days_left > 7) return null;
    if (trial_days_left <= 0) tone = "bad";
  } else if (status === "past_due") {
    text = "No pudimos cobrar tu suscripción. Actualiza tu medio de pago para evitar la suspensión.";
  } else if (status === "cancelled") {
    tone = "bad";
    text = "Tu suscripción está cancelada. Reactívala para volver a operar.";
  } else if (status === "suspended") {
    tone = "bad";
    text = "Tu cuenta está suspendida. Contacta a soporte.";
  }
  if (!text) return null;
  return (
    <div
      role="status"
      className="inline small"
      style={{
        padding: "8px 16px",
        background: tone === "bad" ? "var(--bad-soft)" : "var(--warn-soft)",
        color: tone === "bad" ? "var(--bad)" : "var(--warn)",
        fontWeight: 600,
      }}
    >
      <span>{text}</span>
      {status !== "suspended" && (
        <Link href="/configuraciones/plan" style={{ textDecoration: "underline", color: "inherit" }}>
          Ver plan y facturación
        </Link>
      )}
    </div>
  );
}
