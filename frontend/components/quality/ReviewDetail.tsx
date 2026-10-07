"use client";

import { useState } from "react";
import { fmtDateTime, send } from "@/lib/api";
import { Badge, ErrorBox, useAction } from "@/components/ui";
import { useMe } from "@/components/Shell";
import {
  REVIEW_STATUS,
  SENTIMENT,
  fmtScore,
  isSupervisor,
  scoreTone,
  type Review,
} from "@/lib/quality-types";

/** Detalle de una revisión: puntaje por criterio con evidencia, resumen, coaching, impugnar y resolver. */
export default function ReviewDetail({ review, onChange }: { review: Review; onChange?: (r: Review) => void }) {
  const me = useMe();
  const [run, busy, error] = useAction();
  const [note, setNote] = useState("");
  const [adjust, setAdjust] = useState<Record<string, number>>({});
  const supervisor = isSupervisor(me?.role);
  const mine = me?.id === review.agent_id;
  const [label, tone] = REVIEW_STATUS[review.status];

  async function dispute() {
    const r = await run(() => send<Review>(`/api/quality/reviews/${review.id}/dispute`, "POST", { note }));
    if (r) {
      setNote("");
      onChange?.(r);
    }
  }

  async function resolve(action: "uphold" | "adjust") {
    const scores = Object.fromEntries(Object.entries(adjust).map(([k, v]) => [k, { score: v }]));
    const r = await run(() => send<Review>(`/api/quality/reviews/${review.id}/resolve`, "POST", { action, scores, note }));
    if (r) {
      setNote("");
      setAdjust({});
      onChange?.(r);
    }
  }

  return (
    <div className="stack" style={{ display: "grid", gap: 12 }}>
      <div className="inline" style={{ flexWrap: "wrap", gap: 8 }}>
        <Badge tone={scoreTone(review.total_score)}>Puntaje {fmtScore(review.total_score)}</Badge>
        <Badge tone={tone}>{label}</Badge>
        {review.critical_failed && <Badge tone="bad">Falla crítica</Badge>}
        {review.sentiment && <Badge tone="neutral">{SENTIMENT[review.sentiment]}</Badge>}
        {review.customer_effort != null && <Badge tone="neutral">Esfuerzo del cliente {review.customer_effort}/5</Badge>}
        <span className="muted small">
          {review.scorecard_name} · {review.reviewer_type === "ai" ? "IA" : `Supervisor: ${review.reviewer_name ?? "—"}`} ·{" "}
          {fmtDateTime(review.updated_at)}
        </span>
      </div>
      <div className="small">
        Evaluado: <strong>{review.subject_type === "agent" ? review.agent_name ?? "Asesor" : review.ai_agent_name ?? "Bot"}</strong>
        {" · "}Conversación #{review.conversation_id}{review.contact_name ? ` (${review.contact_name})` : ""}
      </div>
      {review.summary && <p style={{ margin: 0 }}>{review.summary}</p>}
      {review.error && <ErrorBox error={`La revisión falló: ${review.error}`} />}

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Criterio</th>
              <th className="num">Peso</th>
              <th className="num">Puntaje</th>
              <th>Evidencia y comentario</th>
              {supervisor && review.status === "disputed" && <th>Ajustar</th>}
            </tr>
          </thead>
          <tbody>
            {review.scores.map((s) => (
              <tr key={s.key}>
                <td>
                  <span className="strong">{s.label}</span> {s.critical && <Badge tone="warn">Crítico</Badge>}
                </td>
                <td className="num">{s.weight}</td>
                <td className="num">
                  {!s.applies ? <span className="muted">No aplica</span> : <Badge tone={scoreTone(s.score)}>{fmtScore(s.score)}</Badge>}
                </td>
                <td className="small">
                  {s.evidence && <div className="muted">«{s.evidence}»</div>}
                  {s.comment}
                </td>
                {supervisor && review.status === "disputed" && (
                  <td>
                    <input type="number" min={0} max={100} style={{ width: 70 }} placeholder={fmtScore(s.score)}
                      value={adjust[s.key] ?? ""}
                      onChange={(e) => setAdjust({ ...adjust, [s.key]: Number(e.target.value) })} />
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {review.coaching && review.coaching.length > 0 && (
        <div>
          <h3 style={{ margin: "4px 0" }}>Coaching sugerido</h3>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {review.coaching.map((c) => (
              <li key={c.id}>
                <strong>{c.title}:</strong> {c.suggestion}
                {c.example && <div className="muted small">Ejemplo: «{c.example}»</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {review.dispute_note && (
        <div className="small" style={{ whiteSpace: "pre-wrap", background: "var(--warn-soft)", padding: 8, borderRadius: 6 }}>
          <strong>Impugnación:</strong> {review.dispute_note}
        </div>
      )}

      {error && <ErrorBox error={error} />}
      {(mine || supervisor) && review.status === "done" && (
        <div className="inline" style={{ gap: 8 }}>
          <input style={{ flex: 1 }} placeholder="¿No estás de acuerdo? Explica por qué" value={note}
            onChange={(e) => setNote(e.target.value)} />
          <button disabled={busy || !note.trim()} onClick={dispute}>Impugnar</button>
        </div>
      )}
      {supervisor && review.status === "disputed" && (
        <div className="inline" style={{ gap: 8, flexWrap: "wrap" }}>
          <input style={{ flex: 1 }} placeholder="Nota de la resolución" value={note} onChange={(e) => setNote(e.target.value)} />
          <button disabled={busy} onClick={() => resolve("uphold")}>Mantener</button>
          <button className="primary" disabled={busy || !Object.keys(adjust).length} onClick={() => resolve("adjust")}>
            Ajustar puntajes
          </button>
        </div>
      )}
    </div>
  );
}
