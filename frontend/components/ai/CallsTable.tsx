"use client";

import { useState } from "react";
import { fmtDateTime, qs } from "@/lib/api";
import { STATUS_TONE, type AICall, type AIConnection, type CallStatus, type Cortex } from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, useApi } from "@/components/ui";

const STATUSES: CallStatus[] = ["ok", "error", "timeout", "slow", "invalid", "refused"];
const PAGE = 50;

/** Llamadas recientes a LLMs (ai_calls) con filtros; acepta respuesta como lista o {total, items}. */
export default function CallsTable({ connections, cortexes }: { connections: AIConnection[]; cortexes: Cortex[] }) {
  const [f, setF] = useState({ cortex_id: "", connection_id: "", status: "", purpose: "" });
  const [cursors, setCursors] = useState<number[]>([]); // before_id de cada página visitada
  const before = cursors[cursors.length - 1];
  const calls = useApi<AICall[]>(`/api/ai/calls${qs({ ...f, limit: PAGE, before_id: before })}`);
  const items = calls.data ?? [];
  const conn = (id: number | null, name?: string | null) =>
    name ?? (id ? connections.find((c) => c.id === id)?.name ?? `#${id}` : "—");
  const cx = (id: number | null) => (id ? cortexes.find((c) => c.id === id)?.name ?? `#${id}` : "—");
  const upd = (k: keyof typeof f, v: string) => {
    setF((x) => ({ ...x, [k]: v }));
    setCursors([]);
  };

  return (
    <Card
      title="Llamadas recientes"
      actions={
        <>
          <select style={{ width: "auto" }} value={f.cortex_id} onChange={(e) => upd("cortex_id", e.target.value)}>
            <option value="">Todos los Cortex</option>
            {cortexes.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <select style={{ width: "auto" }} value={f.connection_id} onChange={(e) => upd("connection_id", e.target.value)}>
            <option value="">Todas las conexiones</option>
            {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <select style={{ width: "auto" }} value={f.status} onChange={(e) => upd("status", e.target.value)}>
            <option value="">Todos los estados</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select style={{ width: "auto" }} value={f.purpose} onChange={(e) => upd("purpose", e.target.value)}>
            <option value="">Todos los usos</option>
            {["chat", "classification", "learning", "flow", "json_edit", "test", "qa", "agent_test", "onboarding", "golden", "copilot", "assistant", "embedding"].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <button onClick={calls.reload}>Actualizar</button>
        </>
      }
    >
      <ErrorBox error={calls.error} />
      {items.length === 0 ? (
        <Empty>{calls.loading ? "Cargando…" : "Sin llamadas con esos filtros."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Cortex → conexión</th>
                <th>Uso</th>
                <th>Intento</th>
                <th>Estado</th>
                <th className="num">Latencia</th>
                <th className="num">Tokens (ent/sal)</th>
                <th className="num">Costo USD</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={`${c.id}-${c.created_at}`}>
                  <td className="nowrap small">{fmtDateTime(c.created_at)}</td>
                  <td className="small">
                    {cx(c.cortex_id)} → <strong>{conn(c.connection_id, c.connection_name)}</strong>
                    {c.fallback_from_call_id && <div className="muted">failover desde #{c.fallback_from_call_id}</div>}
                  </td>
                  <td className="small">
                    {c.purpose}
                    {c.conversation_id && <div><a href={`/conversaciones?id=${c.conversation_id}`}>conv. {c.conversation_id}</a></div>}
                  </td>
                  <td>{c.attempt}</td>
                  <td><Badge tone={STATUS_TONE[c.status]}>{c.status}</Badge></td>
                  <td className="num">{c.latency_ms ?? "—"} ms</td>
                  <td className="num small">{c.input_tokens ?? "—"} / {c.output_tokens ?? "—"}</td>
                  <td className="num small">{c.cost_usd != null ? Number(c.cost_usd).toFixed(4) : "—"}</td>
                  <td className="small muted" style={{ maxWidth: 260 }}>{c.error?.slice(0, 160) ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <span className="small muted">Página {cursors.length + 1}</span>
        <div className="inline">
          <button disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>‹ Más recientes</button>
          <button disabled={items.length < PAGE} onClick={() => setCursors([...cursors, items[items.length - 1].id])}>Más antiguas ›</button>
        </div>
      </div>
    </Card>
  );
}
