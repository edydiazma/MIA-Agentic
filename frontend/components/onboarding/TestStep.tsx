"use client";

import { useEffect, useState } from "react";
import { send } from "@/lib/api";
import { ErrorBox, Field, useAction } from "@/components/ui";
import { StepFooter, useWizard } from "./common";

type TestResult = { sent: boolean; message_id: string | null; waiting_reply: boolean };

export default function TestStep() {
  const { state, answers, update, reload, next, goTo } = useWizard();
  const phone = answers.test?.phone ?? answers.company?.phone ?? "";
  const [sent, setSent] = useState<TestResult | null>(null);
  const [run, busy, error] = useAction();
  const out = state.checks.find((c) => c.check_key === "outbound_test");
  const back = state.checks.find((c) => c.check_key === "inbound_roundtrip");
  const done = back?.status === "pass";
  const waiting = !done && (sent?.waiting_reply || out?.status === "pass");

  // Mientras esperamos tu respuesta, preguntamos al servidor cada 4 s
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(() => void reload(), 4000);
    return () => clearInterval(t);
  }, [waiting, reload]);

  async function sendTest() {
    const digits = phone.replace(/\D/g, "");
    if (digits.length < 8) return;
    const r = await run(() => send<TestResult>("/api/onboarding/test", "POST", { phone: digits }));
    if (r) {
      setSent(r);
      await reload();
    }
  }

  return (
    <div className="ob-step">
      <section className="card">
        <h3>Prueba de punta a punta</h3>
        <p className="small muted">Enviamos un mensaje real a tu WhatsApp. Respóndelo con cualquier texto y verás cómo llega a la bandeja.</p>
        <div className="ob-test-row">
          <Field label="Tu número de WhatsApp (con indicativo de país)">
            <input value={phone} inputMode="tel" placeholder="+57 300 000 0000" onChange={(e) => update((a) => ({ ...a, test: { ...(a.test ?? {}), phone: e.target.value } }))} />
          </Field>
          <button className="primary" onClick={sendTest} disabled={busy || phone.replace(/\D/g, "").length < 8}>
            {busy ? "Enviando…" : sent || out?.status === "pass" ? "Reenviar" : "Enviar mensaje de prueba"}
          </button>
        </div>
        <ErrorBox error={error || (out?.status === "fail" ? out.detail : null)} />
      </section>

      <section className={`ob-roundtrip ${done ? "done" : waiting ? "waiting" : ""}`} aria-live="polite">
        <div className="ob-rt-node">
          <span aria-hidden>🖥️</span>
          <span className="small">Plataforma</span>
        </div>
        <div className={`ob-rt-line ${out?.status === "pass" || sent?.sent ? "on" : ""}`} aria-hidden />
        <div className="ob-rt-node">
          <span aria-hidden>📱</span>
          <span className="small">Tu teléfono</span>
        </div>
        <div className={`ob-rt-line ${done ? "on" : waiting ? "pulse" : ""}`} aria-hidden />
        <div className="ob-rt-node">
          <span aria-hidden>📥</span>
          <span className="small">Bandeja</span>
        </div>
        <p className="ob-rt-status">
          {done ? "✓ ¡Funciona! Recibimos tu respuesta." : waiting ? "Esperando tu respuesta en WhatsApp…" : "Envía la prueba para empezar."}
        </p>
      </section>

      <StepFooter
        onBack={() => goTo("team")}
        onNext={next}
        nextLabel={done ? "Continuar" : "Continuar sin probar"}
        extra={
          !done ? (
            <button className="link" onClick={() => send("/api/onboarding/steps/test/skip", "POST").then(reload).then(next, next)}>
              Omitir prueba
            </button>
          ) : null
        }
      />
    </div>
  );
}
