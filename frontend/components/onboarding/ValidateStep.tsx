"use client";

import { useEffect, useRef, useState } from "react";
import { send } from "@/lib/api";
import { ErrorBox, useAction } from "@/components/ui";
import type { HealthCheck } from "@/lib/onboarding-types";
import { ChecksList, StepFooter, useWizard } from "./common";

/** Validaciones que deben pasar (o al menos advertir) para seguir; las demás llegan después (pruebas, plantillas). */
const BLOCKING = ["token_valid", "registered", "webhook"];

export default function ValidateStep() {
  const { state, reload, next, goTo } = useWizard();
  const [checks, setChecks] = useState<HealthCheck[]>(state.checks);
  const [fixing, setFixing] = useState<string | null>(null);
  const [run, busy, error] = useAction();
  const started = useRef(false);

  async function runAll() {
    const r = await run(() => send<{ checks: HealthCheck[] }>("/api/onboarding/checks/run", "POST"));
    if (r) {
      setChecks(r.checks);
      await reload();
    }
  }

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void runAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function fix(key: string) {
    setFixing(key);
    const r = await run(() => send<{ check: HealthCheck }>(`/api/onboarding/checks/${key}/fix`, "POST"));
    setFixing(null);
    if (r) {
      setChecks((cs) => cs.map((c) => (c.check_key === key ? r.check : c)));
      await reload();
    }
  }

  const failing = checks.filter((c) => BLOCKING.includes(c.check_key) && c.status === "fail");
  const counts = {
    pass: checks.filter((c) => c.status === "pass").length,
    warn: checks.filter((c) => c.status === "warn").length,
    fail: checks.filter((c) => c.status === "fail").length,
  };

  return (
    <div className="ob-step">
      <section className="card">
        <div className="card-head">
          <div>
            <h3>Revisión automática del número</h3>
            <p className="small muted">
              {busy && !fixing
                ? "Consultando a Meta…"
                : checks.length
                  ? `${counts.pass} correctas · ${counts.warn} con atención · ${counts.fail} con falla`
                  : "Aún no hay resultados."}
            </p>
          </div>
          <button onClick={runAll} disabled={busy}>
            {busy && !fixing ? "Revisando…" : "Volver a revisar"}
          </button>
        </div>
        {busy && !checks.length ? <div className="ob-shimmer tall" aria-hidden /> : <ChecksList checks={checks} onFix={fix} fixing={fixing} />}
        <ErrorBox error={error} />
      </section>
      {failing.length > 0 && (
        <p className="ob-note warn small" role="status">
          Hay validaciones críticas con falla. Usa «Arreglar» o vuelve a{" "}
          <button className="link small" onClick={() => goTo("whatsapp")}>
            conectar el número
          </button>
          .
        </p>
      )}
      <StepFooter onBack={() => goTo("whatsapp")} onNext={next} nextDisabled={busy || failing.length > 0} />
    </div>
  );
}
