"use client";

// Línea de tiempo de la conversación: asignaciones, transferencias, tipificaciones, bot on/off, flujos, etapas, SLA.
import { useState } from "react";
import { useApi } from "@/components/ui";
import type { TimelineItem } from "@/lib/productivity-types";

export default function ConversationTimeline({ conversationId }: { conversationId: number }) {
  const [open, setOpen] = useState(false);
  const tl = useApi<{ items: TimelineItem[] }>(open ? `/api/conversations/${conversationId}/timeline` : null);
  const items = tl.data?.items ?? [];

  return (
    <div>
      <h3>
        <button type="button" className="link" aria-expanded={open} onClick={() => setOpen((v) => !v)}
          style={{ font: "inherit", color: "inherit" }}>
          {open ? "▾" : "▸"} Línea de tiempo
        </button>
      </h3>
      {open && (
        <div style={{ marginTop: 8 }}>
          {tl.error && <div className="error small">{tl.error}</div>}
          {!tl.data && !tl.error && <div className="muted small">Cargando…</div>}
          {tl.data && items.length === 0 && <div className="muted small">Sin eventos todavía</div>}
          <ol className="tl">
            {items.map((i, idx) => (
              <li key={`${i.type}-${i.at}-${idx}`}>
                <span aria-hidden>{i.icon}</span>
                <span>
                  <span className="strong">{i.label}</span>
                  {i.detail && <span> · {i.detail}</span>}
                  <div className="tl-meta">
                    {new Date(i.at).toLocaleString("es", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                    {i.actor && ` · ${i.actor}`}
                  </div>
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
