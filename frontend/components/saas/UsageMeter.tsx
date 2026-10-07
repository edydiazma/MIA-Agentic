import { fmtLimit, type Usage } from "@/lib/saas-types";

/** Barra de consumo de un límite del plan (verde → ámbar desde 80 % → rojo al tope). */
export function UsageMeter({ label, usage }: { label: string; usage: Usage }) {
  const pct = usage.limit ? Math.min(100, (usage.used / usage.limit) * 100) : 0;
  const color = !usage.allowed || pct >= 100 ? "var(--bad)" : pct >= 80 ? "var(--warn)" : "var(--accent)";
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <div className="row small">
        <span>{label}</span>
        <span className="muted">
          {usage.used.toLocaleString("es")} / {fmtLimit(usage.limit)}
        </span>
      </div>
      <div className="bar-track" style={{ height: 8 }}>
        <div className="bar-fill" style={{ width: `${usage.limit == null ? 0 : pct}%`, background: color }} />
      </div>
    </div>
  );
}
