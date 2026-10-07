"use client";

import { useState } from "react";
import { fmtDateTime, timeAgo } from "@/lib/api";
import { Badge, Card, Empty } from "@/components/ui";
import { STATUS_META, type CheckArea, type CheckResult, type CheckStatus, type PreflightView } from "@/lib/preflight-types";

/** Lista agrupada por área con estado, detalle y arreglo; filtro de fallas, reverificación y copia del informe. */
export default function PreflightReport({
  title,
  view,
  busy,
  onRun,
}: {
  title: string;
  view: PreflightView | null;
  busy: boolean;
  /** areas undefined = todas */
  onRun: (areas?: string[]) => void;
}) {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [copied, setCopied] = useState(false);
  const run = view?.run ?? null;
  const running = busy || !!run?.running;
  const areas = (view?.areas ?? [])
    .map((a) => ({ ...a, results: onlyProblems ? a.results.filter((r) => r.status === "fail" || r.status === "warn") : a.results }))
    .filter((a) => a.results.length > 0);

  async function copy() {
    const lines = [`${title} — ${run?.finished_at ? fmtDateTime(run.finished_at) : "sin ejecutar"}`];
    for (const a of view?.areas ?? []) {
      lines.push("", `## ${a.label}`);
      for (const r of a.results) lines.push(`[${STATUS_META[r.status].label}] ${r.label}${r.detail ? `: ${r.detail}` : ""}`);
    }
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* el navegador no permite el portapapeles: no pasa nada */
    }
  }

  return (
    <Card
      title={title}
      actions={
        <div className="inline">
          <label className="inline small">
            <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> Solo fallas y advertencias
          </label>
          <button onClick={copy} disabled={!view?.areas.length}>{copied ? "Copiado" : "Copiar informe"}</button>
          <button className="primary" onClick={() => onRun()} disabled={running}>
            {running ? "Verificando…" : run ? "Volver a verificar" : "Verificar ahora"}
          </button>
        </div>
      }
    >
      {run && (
        <div className="stats" style={{ marginBottom: 12 }}>
          <Summary label="Correctas" n={run.passed} tone="ok" />
          <Summary label="Advertencias" n={run.warned} tone="warn" />
          <Summary label="Fallas" n={run.failed} tone="bad" />
          <Summary label="Omitidas" n={run.skipped} tone="neutral" />
          <div className="small muted" style={{ alignSelf: "center" }}>
            {run.running ? "En curso…" : <>Última verificación {timeAgo(run.finished_at)} · {fmtDateTime(run.finished_at)}</>}
            {run.app_version && <> · versión {run.app_version}</>}
          </div>
        </div>
      )}
      {!view?.areas.length ? (
        <Empty>{running ? "Verificando integraciones…" : "Aún no hay resultados. Pulsa «Verificar ahora»: solo hace consultas de lectura, no envía mensajes ni cobra."}</Empty>
      ) : areas.length === 0 ? (
        <Empty>Sin fallas ni advertencias. 🎉</Empty>
      ) : (
        <div className="preflight-areas">
          {areas.map((a) => <AreaBlock key={a.area} area={a} disabled={running} onRun={() => onRun([a.area])} />)}
        </div>
      )}
    </Card>
  );
}

function Summary({ label, n, tone }: { label: string; n: number; tone: "ok" | "warn" | "bad" | "neutral" }) {
  return (
    <div className="stat">
      <span className="muted small">{label}</span>
      <Badge tone={tone}>{n}</Badge>
    </div>
  );
}

function worst(results: CheckResult[]): CheckStatus {
  for (const s of ["fail", "warn", "pass"] as CheckStatus[]) if (results.some((r) => r.status === s)) return s;
  return "skipped";
}

function AreaBlock({ area, disabled, onRun }: { area: CheckArea; disabled: boolean; onRun: () => void }) {
  const status = worst(area.results);
  return (
    <section className="preflight-area" aria-label={area.label}>
      <header className="row">
        <h3 className="strong" style={{ margin: 0 }}>
          <span aria-hidden className={`pf-icon ${status}`}>{STATUS_META[status].icon}</span> {area.label}
        </h3>
        <button className="link small" onClick={onRun} disabled={disabled}>Verificar esta área</button>
      </header>
      <ul className="preflight-list">
        {area.results.map((r) => (
          <li key={r.check_key} className={`pf-${r.status}`}>
            <span aria-hidden className={`pf-icon ${r.status}`}>{STATUS_META[r.status].icon}</span>
            <div style={{ minWidth: 0 }}>
              <div>
                <span className="strong">{r.label}</span>{" "}
                <Badge tone={STATUS_META[r.status].tone}>{STATUS_META[r.status].label}</Badge>
                {r.latency_ms != null && <span className="muted small"> · {r.latency_ms} ms</span>}
              </div>
              {r.detail && <div className="small">{r.detail}</div>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
