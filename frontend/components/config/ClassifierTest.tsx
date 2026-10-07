"use client";

import { useState } from "react";
import {
  contactLabel,
  fmtDateTime,
  qs,
  send,
  SENTIMENT_LABEL,
  STATUS_LABEL,
  type ClassifierSettings,
  type ClassifyResponse,
  type Conversation,
} from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, useAction, useApi } from "@/components/ui";

const pctLabel = (n: number) => `${Math.round(n * 100)} %`;

function Confidence({ value, threshold }: { value: number; threshold: number }) {
  return <Badge tone={value >= threshold ? "ok" : "neutral"}>{pctLabel(value)}</Badge>;
}

/** Prueba la configuración del formulario (aunque no esté guardada) contra una conversación real. */
export default function ClassifierTest({ draft }: { draft: ClassifierSettings }) {
  const [q, setQ] = useState("");
  const { data: convs } = useApi<Conversation[]>(`/api/conversations${qs({ q, limit: 20 })}`);
  const [selected, setSelected] = useState<number | null>(null);
  const [out, setOut] = useState<ClassifyResponse | null>(null);
  const [run, busy, error] = useAction();
  const threshold = draft.min_confidence;

  async function test() {
    if (!selected) return;
    setOut(null);
    const { api_key, has_api_key: _h, clear_api_key: _c, ...config } = draft;
    const body = { conversation_id: selected, config: api_key ? { ...config, api_key } : config };
    const r = await run(() => send<ClassifyResponse>("/api/classifier/test", "POST", body));
    if (r) setOut(r);
  }

  const res = out?.result;
  return (
    <Card title="Probar" actions={<span className="small muted">Usa la configuración del formulario, aunque no esté guardada. No cambia nada.</span>}>
      <div className="grid2" style={{ alignItems: "start" }}>
        <div className="stack">
          <input placeholder="Buscar conversación por nombre o teléfono" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="table-wrap" style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 8 }}>
            <table className="table">
              <tbody>
                {(convs ?? []).map((c) => (
                  <tr key={c.id} className="clickable" onClick={() => setSelected(c.id)}
                      style={selected === c.id ? { background: "var(--accent-soft)" } : undefined}>
                    <td>
                      <div className="strong">{contactLabel(c.contact)}</div>
                      <div className="small muted preview" style={{ maxWidth: 260 }}>{c.last_message_preview}</div>
                    </td>
                    <td className="small muted nowrap">
                      {STATUS_LABEL[c.status]}
                      <br />
                      {fmtDateTime(c.last_message_at)}
                    </td>
                  </tr>
                ))}
                {convs && convs.length === 0 && (
                  <tr><td className="muted">Sin conversaciones</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="inline">
            <button className="primary" disabled={!selected || busy} onClick={test}>
              {busy ? "Analizando…" : "Analizar conversación"}
            </button>
            {out?.latency_ms != null && <span className="small muted">Respondió en {(out.latency_ms / 1000).toFixed(1)} s</span>}
          </div>
          <ErrorBox error={error} />
        </div>

        <div>
          {!out && !busy && <Empty>Elige una conversación y pulsa «Analizar».</Empty>}
          {out?.skipped && <Empty>{out.skipped}</Empty>}
          {res && (
            <div className="stack">
              <div>
                <div className="small muted">Resumen</div>
                <div>{res.summary || "—"}</div>
              </div>
              <div className="inline">
                {res.sentiment && <Badge tone={res.sentiment === "negative" ? "bad" : res.sentiment === "positive" ? "ok" : "neutral"}>{SENTIMENT_LABEL[res.sentiment]}</Badge>}
              </div>
              <dl className="kv">
                <dt>Etiquetas</dt>
                <dd>
                  {res.tags.length ? res.tags.map((t) => (
                    <span key={t.name} className="inline" style={{ display: "inline-flex", marginRight: 8 }}>
                      <span className="tag">{t.name}</span><Confidence value={t.confidence} threshold={threshold} />
                    </span>
                  )) : "—"}
                </dd>
                <dt>Tipificación</dt>
                <dd>{res.typification ? <>{res.typification} <Confidence value={res.typification_confidence} threshold={threshold} /></> : "—"}</dd>
                <dt>Grupo</dt>
                <dd>{res.group ? <>{res.group} <Confidence value={res.group_confidence} threshold={threshold} /></> : "—"}</dd>
              </dl>
              {res.fields.length > 0 && (
                <div className="table-wrap">
                  <table className="table">
                    <thead><tr><th>Campo</th><th>Valor</th><th>Evidencia</th><th className="num">Confianza</th></tr></thead>
                    <tbody>
                      {res.fields.map((f) => (
                        <tr key={f.key}>
                          <td>{f.key}</td>
                          <td>{String(f.value)}</td>
                          <td className="small muted">{f.evidence ? `«${f.evidence}»` : "—"}</td>
                          <td className="num"><Confidence value={f.confidence} threshold={threshold} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {res.reason && <p className="small muted" style={{ margin: 0 }}>Motivo: {res.reason}</p>}
              <p className="small muted" style={{ margin: 0 }}>
                En verde, lo que supera la confianza mínima ({pctLabel(threshold)}) y se aplicaría según los modos configurados.
              </p>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
