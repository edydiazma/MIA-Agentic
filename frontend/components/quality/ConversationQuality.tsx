"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import { Badge, ErrorBox, Modal, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import ReviewDetail from "@/components/quality/ReviewDetail";
import {
  REVIEW_STATUS,
  SENTIMENT,
  fmtScore,
  isSupervisor,
  scoreTone,
  type ConversationQualityData,
  type Review,
} from "@/lib/quality-types";

/** Bloque «Calidad» del panel del cliente en el inbox: última revisión y acceso al detalle.
 *  Si el plan no incluye QA (402) no muestra nada. */
export default function ConversationQuality({ conversationId, closed }: { conversationId: number; closed?: boolean }) {
  const me = useMe();
  const data = useApi<ConversationQualityData>(`/api/quality/conversations/${conversationId}`);
  const [open, setOpen] = useState<Review | null>(null);
  const [run, busy, error] = useAction();

  if (data.error) return null;
  const reviews = data.data?.reviews ?? [];
  const latest = reviews[0];
  const supervisor = isSupervisor(me?.role);

  async function review() {
    if (await run(() => send(`/api/quality/conversations/${conversationId}/review`, "POST", {}))) data.reload();
  }

  return (
    <div>
      <div className="row">
        <h3>Calidad</h3>
        {supervisor && (
          <button className="link small" disabled={busy} onClick={review}>
            {busy ? "Revisando…" : latest ? "Revisar de nuevo" : "Revisar con IA"}
          </button>
        )}
      </div>
      {error && <ErrorBox error={error} />}
      {!latest ? (
        <p className="muted small" style={{ margin: "4px 0" }}>
          {closed ? "Sin revisión todavía (se evalúa automáticamente al cerrar, según la muestra)." : "Se evalúa al cerrar la conversación."}
        </p>
      ) : (
        <div style={{ display: "grid", gap: 4, marginTop: 4 }}>
          <div className="inline" style={{ flexWrap: "wrap", gap: 6 }}>
            <Badge tone={scoreTone(latest.total_score)}>Puntaje {fmtScore(latest.total_score)}</Badge>
            {latest.critical_failed && <Badge tone="bad">Falla crítica</Badge>}
            {latest.sentiment && <Badge tone="neutral">{SENTIMENT[latest.sentiment]}</Badge>}
            {latest.status !== "done" && <Badge tone={REVIEW_STATUS[latest.status][1]}>{REVIEW_STATUS[latest.status][0]}</Badge>}
          </div>
          {latest.summary && <span className="small">{latest.summary}</span>}
          <span className="small">
            {reviews.map((r) => (
              <button key={r.id} className="link small" style={{ marginRight: 8 }} onClick={() => setOpen(r)}>
                {r.scorecard_name}{r.reviewer_type === "human" ? " (manual)" : ""} →
              </button>
            ))}
          </span>
        </div>
      )}
      {open && (
        <Modal title={`Revisión · ${open.scorecard_name}`} onClose={() => setOpen(null)} wide>
          <ReviewDetail review={open} onChange={(r) => { setOpen(r); data.reload(); }} />
        </Modal>
      )}
    </div>
  );
}
