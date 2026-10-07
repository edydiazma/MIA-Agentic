"use client";

import { useState } from "react";
import { fmtNum } from "@/lib/api";
import { Badge } from "@/components/ui";
import { copy } from "@/components/config/common";
import { UPLOAD_STATUS, type UploadStatus } from "@/lib/attribution-types";

/** Bloque de código con botón «Copiar». */
export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="inline" style={{ justifyContent: "space-between" }}>
        {label && <span className="small strong">{label}</span>}
        <button
          className="small"
          onClick={() => {
            copy(code);
            setDone(true);
            setTimeout(() => setDone(false), 1500);
          }}
        >
          {done ? "Copiado ✓" : "Copiar"}
        </button>
      </div>
      <pre
        style={{
          margin: 0,
          padding: 10,
          overflowX: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          fontSize: 12,
          background: "var(--bg-subtle, rgba(127,127,127,.08))",
          borderRadius: 6,
        }}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function UploadBadge({ status }: { status: UploadStatus }) {
  const s = UPLOAD_STATUS[status] ?? { label: status, tone: "neutral" as const };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

/** Valor monetario (sin decimales para COP y similares). */
export function fmtMoney(n: number | null | undefined, currency = "COP") {
  if (n == null) return "—";
  try {
    return n.toLocaleString("es", { style: "currency", currency, maximumFractionDigits: n % 1 ? 2 : 0 });
  } catch {
    return `${fmtNum(n)} ${currency}`;
  }
}

/** Mensaje cuando el plan no incluye atribución (402) para mostrar en lugar del error crudo. */
export function planError(error: string | null | undefined) {
  if (!error) return error;
  return /402|plan/i.test(error) ? "Tu plan no incluye atribución y conversiones. Actualízalo en Configuraciones › Plan." : error;
}
