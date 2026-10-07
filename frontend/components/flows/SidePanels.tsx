"use client";

import { useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Modal, useAction } from "@/components/ui";
import { fmtDateTime, timeAgo } from "@/lib/api";
import type { FlowDefinition, FlowTestResult, FlowVariable, FlowVersion } from "@/lib/flow-types";
import { flowsApi } from "./api";
import styles from "./flows.module.css";

// ---------------------------------------------------------------------------
// Simulador: escribe un mensaje de cliente y mira qué haría el flujo (sin enviar nada)
// ---------------------------------------------------------------------------
export function Simulator({ flowId, definition }: { flowId: number; definition: FlowDefinition }) {
  const [text, setText] = useState("");
  const [log, setLog] = useState<{ input: string; result: FlowTestResult }[]>([]);
  const [run, busy, error] = useAction();

  async function test(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    const input = text.trim();
    const result = await run(() => flowsApi.test(flowId, input, definition));
    if (result) {
      setLog((l) => [...l, { input, result }]);
      setText("");
    }
  }

  return (
    <Card title="Probar" actions={log.length > 0 && <button className="link small" onClick={() => setLog([])}>Limpiar</button>}>
      <p className="small muted" style={{ marginTop: 0 }}>
        Simula un mensaje del cliente con la versión que estás editando. No se envía nada por WhatsApp.
      </p>
      <div className={styles.simLog}>
        {log.length === 0 && <span className="small muted">Aún no hay pruebas.</span>}
        {log.map((entry, i) => (
          <div key={i} className="stack" style={{ gap: 4 }}>
            <div className="small">
              <strong>Cliente:</strong> {entry.input}
            </div>
            {entry.result.steps.map((s, j) => (
              <div key={j} className={styles.simStep}>
                <Badge tone={s.status === "ok" ? "ok" : s.status === "error" ? "bad" : s.status === "waiting" ? "warn" : "neutral"}>
                  {s.status}
                </Badge>
                <span>{s.type}</span>
                {s.error && <span className="error">{s.error}</span>}
              </div>
            ))}
            {entry.result.messages.map((m, j) => (
              <div key={`m${j}`} className={styles.simMsg}>
                🤖 {m.text}
              </div>
            ))}
            {entry.result.waiting && <span className="small muted">⏳ El flujo queda esperando la respuesta del cliente.</span>}
          </div>
        ))}
      </div>
      <ErrorBox error={error} />
      <form onSubmit={test} className="inline" style={{ marginTop: 8 }}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Mensaje del cliente…" style={{ flex: 1 }} />
        <button className="primary" disabled={busy || !text.trim()}>
          {busy ? "…" : "Probar"}
        </button>
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Variables del flujo
// ---------------------------------------------------------------------------
export function VariablesPanel({
  variables,
  onChange,
  readOnly,
}: {
  variables: FlowVariable[];
  onChange: (v: FlowVariable[]) => void;
  readOnly?: boolean;
}) {
  const set = (i: number, patch: Partial<FlowVariable>) => onChange(variables.map((v, j) => (j === i ? { ...v, ...patch } : v)));
  return (
    <Card
      title="Variables"
      actions={
        !readOnly && (
          <button className="small" onClick={() => onChange([...variables, { name: `var${variables.length + 1}`, type: "text", default: null }])}>
            + Variable
          </button>
        )
      }
    >
      {variables.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          Úsalas en los textos como <code>{"{{vars.nombre}}"}</code>.
        </p>
      ) : (
        <div className="stack" style={{ gap: 6 }}>
          {variables.map((v, i) => (
            <div key={i} className={styles.varRow}>
              <input
                disabled={readOnly}
                value={v.name}
                aria-label="Nombre"
                onChange={(e) => set(i, { name: e.target.value.replace(/[^a-zA-Z0-9_]/g, "") })}
              />
              <select disabled={readOnly} value={v.type} onChange={(e) => set(i, { type: e.target.value as FlowVariable["type"] })}>
                <option value="text">texto</option>
                <option value="number">número</option>
                <option value="boolean">sí/no</option>
              </select>
              <input
                disabled={readOnly}
                placeholder="valor inicial"
                value={v.default == null ? "" : String(v.default)}
                onChange={(e) =>
                  set(i, {
                    default:
                      e.target.value === ""
                        ? null
                        : v.type === "number"
                          ? Number(e.target.value)
                          : v.type === "boolean"
                            ? e.target.value === "true"
                            : e.target.value,
                  })
                }
              />
              {!readOnly && (
                <button className="icon" aria-label="Quitar" onClick={() => onChange(variables.filter((_, j) => j !== i))}>
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Versiones
// ---------------------------------------------------------------------------
export function VersionsDrawer({
  flowId,
  currentVersionId,
  onClose,
  onLoad,
}: {
  flowId: number;
  currentVersionId: number | null;
  onClose: () => void;
  onLoad: (definition: FlowDefinition, version: FlowVersion) => void;
}) {
  const [versions, setVersions] = useState<FlowVersion[] | null>(null);
  const [run, busy, error] = useAction();
  useEffect(() => {
    run(async () => setVersions(await flowsApi.versions(flowId)));
  }, [flowId, run]);

  async function load(v: FlowVersion) {
    const detail = await run(() => flowsApi.version(flowId, v.id));
    if (detail) {
      onLoad(detail.definition, v);
      onClose();
    }
  }

  return (
    <Modal title="Versiones" onClose={onClose} wide>
      <ErrorBox error={error} />
      {versions === null ? (
        <div className="empty-state muted">Cargando…</div>
      ) : versions.length === 0 ? (
        <Empty>Aún no hay versiones guardadas.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Versión</th>
                <th>Nota</th>
                <th>Origen</th>
                <th>Fecha</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.id}>
                  <td className="nowrap">
                    v{v.version} {v.id === currentVersionId && <Badge tone="ok">publicada</Badge>}
                  </td>
                  <td>
                    {v.change_note || <span className="muted">—</span>}
                    {v.ai_prompt && <div className="small muted">IA: «{v.ai_prompt}»</div>}
                  </td>
                  <td>{v.created_by_ai ? <Badge tone="info">🤖 IA</Badge> : <Badge>Persona</Badge>}</td>
                  <td className="nowrap" title={fmtDateTime(v.created_at)}>
                    {timeAgo(v.created_at)}
                  </td>
                  <td>
                    <button disabled={busy} onClick={() => load(v)}>
                      Cargar
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
