"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import {
  STATUS_TONE,
  type AIConnection,
  type Attempt,
  type Cortex,
  type CortexIn,
  type CortexMember,
  type CortexPurpose,
  type CortexStrategy,
  type CortexTestResult,
} from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, Field, Modal, Toggle, useAction } from "@/components/ui";
import JsonEditWithAI from "./JsonEditWithAI";

const STRATEGY_LABEL: Record<CortexStrategy, string> = {
  failover: "Failover por orden",
  lowest_latency: "Menor latencia",
  weighted: "Ponderado",
};
const PURPOSE_LABEL: Record<CortexPurpose, string> = {
  any: "Cualquiera",
  chat: "Chat (agentes)",
  classification: "Clasificación",
  learning: "Aprendizaje",
  flow: "Flujos",
  json_edit: "Edición JSON",
  qa: "Calidad (QA)",
  agent_test: "Pruebas de agentes",
};

const EMPTY: CortexIn = {
  name: "",
  description: "",
  purpose: "any",
  strategy: "failover",
  max_latency_ms: 15000,
  max_attempts: 3,
  circuit_breaker_failures: 5,
  circuit_breaker_cooldown_s: 120,
  validation: { min_chars: null, max_chars: null, banned_phrases: [] },
  is_active: true,
  members: [],
};

export function AttemptsTimeline({ attempts }: { attempts: Attempt[] }) {
  if (!attempts.length) return <p className="muted small">Sin intentos registrados.</p>;
  return (
    <ol className="stack" style={{ gap: 6, paddingLeft: 18, margin: 0 }}>
      {attempts.map((a, i) => (
        <li key={i}>
          <span className="strong">{a.connection}</span> <span className="muted small">({a.model})</span>{" "}
          <Badge tone={STATUS_TONE[a.status]}>{a.status}</Badge> <span className="small">{a.latency_ms} ms</span>
          {a.error && <div className="small muted">{a.error}</div>}
          {i < attempts.length - 1 && <div className="small muted">↓ failover a la siguiente conexión</div>}
        </li>
      ))}
    </ol>
  );
}

function CortexModal({
  initial,
  connections,
  onClose,
  onSaved,
}: {
  initial: Cortex | null;
  connections: AIConnection[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<CortexIn>(
    initial
      ? { ...initial, members: initial.members.map(({ connection_id, position, weight, timeout_ms }) => ({ connection_id, position, weight, timeout_ms })) }
      : { ...EMPTY, members: [] },
  );
  const [banned, setBanned] = useState((initial?.validation.banned_phrases ?? []).join("\n"));
  const [run, busy, error] = useAction();
  const set = <K extends keyof CortexIn>(k: K, v: CortexIn[K]) => setForm((f) => ({ ...f, [k]: v }));
  const members = [...form.members].sort((a, b) => a.position - b.position);
  const unused = connections.filter((c) => !members.some((m) => m.connection_id === c.id));

  const setMembers = (list: CortexMember[]) => set("members", list.map((m, i) => ({ ...m, position: i + 1 })));
  const move = (i: number, d: -1 | 1) => {
    const list = [...members];
    const j = i + d;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    setMembers(list);
  };

  async function save() {
    const body: CortexIn = {
      ...form,
      members,
      validation: {
        ...form.validation,
        banned_phrases: banned.split("\n").map((s) => s.trim()).filter(Boolean),
      },
    };
    const r = await run(() => (initial ? send(`/api/ai/cortexes/${initial.id}`, "PUT", body) : send("/api/ai/cortexes", "POST", body)));
    if (r !== undefined) onSaved();
  }

  const connName = (id: number) => connections.find((c) => c.id === id)?.name ?? `#${id}`;

  return (
    <Modal
      wide
      title={initial ? `Cortex: ${initial.name}` : "Nuevo Cortex"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !form.name.trim() || !members.length} onClick={save}>Guardar</button>
        </>
      }
    >
      <div className="grid3">
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Uso">
          <select value={form.purpose} onChange={(e) => set("purpose", e.target.value as CortexPurpose)}>
            {(Object.keys(PURPOSE_LABEL) as CortexPurpose[]).map((p) => (
              <option key={p} value={p}>{PURPOSE_LABEL[p]}</option>
            ))}
          </select>
        </Field>
        <Field label="Estrategia">
          <select value={form.strategy} onChange={(e) => set("strategy", e.target.value as CortexStrategy)}>
            {(Object.keys(STRATEGY_LABEL) as CortexStrategy[]).map((s) => (
              <option key={s} value={s}>{STRATEGY_LABEL[s]}</option>
            ))}
          </select>
        </Field>
      </div>
      <Field label="Descripción">
        <input value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} />
      </Field>

      <div>
        <div className="row">
          <strong>Conexiones (en orden de intento)</strong>
          {unused.length > 0 && (
            <select
              value=""
              style={{ width: "auto" }}
              onChange={(e) => {
                if (!e.target.value) return;
                setMembers([...members, { connection_id: Number(e.target.value), position: members.length + 1, weight: 1, timeout_ms: null }]);
              }}
            >
              <option value="">+ Agregar conexión</option>
              {unused.map((c) => (
                <option key={c.id} value={c.id}>{c.name} · {c.model}</option>
              ))}
            </select>
          )}
        </div>
        {members.length === 0 ? (
          <p className="muted small">Agrega al menos una conexión. Con dos o más, el Cortex hace failover.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Conexión</th>
                {form.strategy === "weighted" && <th className="num">Peso</th>}
                <th className="num">Timeout propio (ms)</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {members.map((m, i) => (
                <tr key={m.connection_id}>
                  <td>{i + 1}</td>
                  <td>{connName(m.connection_id)}</td>
                  {form.strategy === "weighted" && (
                    <td className="num">
                      <input type="number" min={1} style={{ width: 80 }} value={m.weight}
                        onChange={(e) => setMembers(members.map((x) => (x === m ? { ...x, weight: Number(e.target.value) || 1 } : x)))} />
                    </td>
                  )}
                  <td className="num">
                    <input type="number" min={1000} step={1000} style={{ width: 110 }} placeholder="de la conexión" value={m.timeout_ms ?? ""}
                      onChange={(e) => setMembers(members.map((x) => (x === m ? { ...x, timeout_ms: e.target.value ? Number(e.target.value) : null } : x)))} />
                  </td>
                  <td className="nowrap">
                    <button className="icon" aria-label="Subir" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                    <button className="icon" aria-label="Bajar" disabled={i === members.length - 1} onClick={() => move(i, 1)}>↓</button>
                    <button className="icon" aria-label="Quitar" onClick={() => setMembers(members.filter((x) => x !== m))}>✕</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="grid4">
        <Field label="Presupuesto de latencia (ms)" hint="Si una conexión lo excede, pasa a la siguiente (el último intento usa el timeout completo).">
          <input type="number" min={0} step={500} value={form.max_latency_ms ?? ""} onChange={(e) => set("max_latency_ms", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field label="Intentos máximos">
          <input type="number" min={1} max={10} value={form.max_attempts} onChange={(e) => set("max_attempts", Number(e.target.value))} />
        </Field>
        <Field label="Pausar tras N fallas" hint="Circuit breaker">
          <input type="number" min={1} value={form.circuit_breaker_failures} onChange={(e) => set("circuit_breaker_failures", Number(e.target.value))} />
        </Field>
        <Field label="Pausa (segundos)">
          <input type="number" min={1} value={form.circuit_breaker_cooldown_s} onChange={(e) => set("circuit_breaker_cooldown_s", Number(e.target.value))} />
        </Field>
      </div>

      <Card title="Respuesta fuera de rango → siguiente conexión">
        <div className="grid2">
          <Field label="Mínimo de caracteres">
            <input type="number" min={0} value={form.validation.min_chars ?? ""}
              onChange={(e) => set("validation", { ...form.validation, min_chars: e.target.value ? Number(e.target.value) : null })} />
          </Field>
          <Field label="Máximo de caracteres">
            <input type="number" min={0} value={form.validation.max_chars ?? ""}
              onChange={(e) => set("validation", { ...form.validation, max_chars: e.target.value ? Number(e.target.value) : null })} />
          </Field>
        </div>
        <Field label="Frases prohibidas (una por línea)" hint="Si la respuesta las contiene, se descarta y se intenta con otra conexión. El JSON inválido siempre cuenta como fuera de rango.">
          <textarea rows={3} value={banned} onChange={(e) => setBanned(e.target.value)} />
        </Field>
      </Card>
      <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label="Cortex activo" />
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function CortexPanel({
  cortexes,
  connections,
  isAdmin,
  reload,
}: {
  cortexes: Cortex[];
  connections: AIConnection[];
  isAdmin: boolean;
  reload: () => void;
}) {
  const [editing, setEditing] = useState<Cortex | "new" | null>(null);
  const [aiEdit, setAiEdit] = useState<Cortex | null>(null);
  const [test, setTest] = useState<{ cortex: Cortex; result: CortexTestResult } | null>(null);
  const [run, busy, error] = useAction();
  const connName = (id: number) => connections.find((c) => c.id === id)?.name ?? `#${id}`;

  async function runTest(c: Cortex) {
    const r = await run(() => send<CortexTestResult>(`/api/ai/cortexes/${c.id}/test`, "POST"));
    if (r) setTest({ cortex: c, result: r });
    reload();
  }

  async function remove(c: Cortex) {
    if (!confirm(`¿Eliminar el Cortex «${c.name}»? Los agentes que lo usan pasarán al automático.`)) return;
    await run(() => send(`/api/ai/cortexes/${c.id}`, "DELETE"));
    reload();
  }

  return (
    <Card title="Cortex (enrutamiento con failover)" actions={isAdmin ? <button className="primary" onClick={() => setEditing("new")}>Nuevo Cortex</button> : undefined}>
      <ErrorBox error={error} />
      {cortexes.length === 0 ? (
        <Empty>No hay Cortex. Crea uno y agrega dos o más conexiones para tener respaldo.</Empty>
      ) : (
        <div className="stack">
          {cortexes.map((c) => (
            <div key={c.id} className="card" style={{ marginTop: 0 }}>
              <div className="row">
                <div>
                  <strong>{c.name}</strong> <Badge tone={c.is_active ? "ok" : "neutral"}>{c.is_active ? "Activo" : "Inactivo"}</Badge>{" "}
                  <Badge tone="info">{PURPOSE_LABEL[c.purpose]}</Badge>
                  {c.description && <div className="small muted">{c.description}</div>}
                </div>
                <div className="actions">
                  <button disabled={busy} onClick={() => runTest(c)}>Probar</button>
                  {isAdmin && (
                    <>
                      <button onClick={() => setAiEdit(c)}>✨ Editar con IA</button>
                      <button onClick={() => setEditing(c)}>Editar</button>
                      <button className="danger" onClick={() => remove(c)}>Eliminar</button>
                    </>
                  )}
                </div>
              </div>
              <div className="small" style={{ marginTop: 6 }}>
                {STRATEGY_LABEL[c.strategy]}:{" "}
                {[...c.members].sort((a, b) => a.position - b.position).map((m) => connName(m.connection_id)).join(" → ") || "sin conexiones"}
              </div>
              <div className="small muted">
                Latencia máx. {c.max_latency_ms ? `${c.max_latency_ms} ms` : "sin límite"} · {c.max_attempts} intentos · pausa tras{" "}
                {c.circuit_breaker_failures} fallas por {c.circuit_breaker_cooldown_s} s
                {(c.validation.min_chars || c.validation.max_chars || c.validation.banned_phrases?.length) && " · con reglas de validación"}
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <CortexModal
          initial={editing === "new" ? null : editing}
          connections={connections}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      {aiEdit && (
        <JsonEditWithAI entityType="cortex" entityId={aiEdit.id} title={`Editar «${aiEdit.name}» con IA`} onClose={() => setAiEdit(null)} onApplied={reload} />
      )}
      {test && (
        <Modal title={`Prueba de «${test.cortex.name}»`} onClose={() => setTest(null)}>
          <p style={{ margin: 0 }}>
            {test.result.ok ? <Badge tone="ok">Respondió</Badge> : <Badge tone="bad">Sin respuesta válida</Badge>}
            {test.result.error && <span className="small muted"> {test.result.error}</span>}
          </p>
          <AttemptsTimeline attempts={test.result.attempts ?? []} />
          {test.result.answer !== undefined && (
            <pre className="small" style={{ background: "var(--panel-2)", padding: 10, borderRadius: 8, overflow: "auto", whiteSpace: "pre-wrap" }}>
              {typeof test.result.answer === "string" ? test.result.answer : JSON.stringify(test.result.answer, null, 2)}
            </pre>
          )}
        </Modal>
      )}
    </Card>
  );
}
