"use client";

import { useState } from "react";
import { send, timeAgo } from "@/lib/api";
import {
  HEALTH_LABEL,
  MODEL_SUGGESTIONS,
  PROVIDER_LABEL,
  type AIConnection,
  type AIConnectionIn,
  type AIProvider,
  type ConnectionHealth,
  type CortexTestResult,
} from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, Field, Modal, Toggle, useAction } from "@/components/ui";

const EMPTY: AIConnectionIn = {
  name: "",
  provider: "anthropic",
  model: "claude-opus-5-5",
  base_url: "",
  default_params: { effort: "low" },
  timeout_ms: 60000,
  input_cost_per_mtok: null,
  output_cost_per_mtok: null,
  is_active: true,
  api_key: "",
};

export function HealthBadge({ h }: { h: ConnectionHealth | null | undefined }) {
  if (!h) return <Badge tone="neutral">Sin uso</Badge>;
  const tone = h.state === "closed" ? "ok" : h.state === "open" ? "bad" : "warn";
  return (
    <span title={h.last_error ?? undefined}>
      <Badge tone={tone}>{HEALTH_LABEL[h.state]}</Badge>
      {h.consecutive_failures > 0 && <span className="small muted"> · {h.consecutive_failures} fallas</span>}
    </span>
  );
}

function ConnectionModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: AIConnection | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<AIConnectionIn>(
    initial
      ? {
          name: initial.name, provider: initial.provider, model: initial.model, base_url: initial.base_url ?? "",
          default_params: initial.default_params, timeout_ms: initial.timeout_ms,
          input_cost_per_mtok: initial.input_cost_per_mtok, output_cost_per_mtok: initial.output_cost_per_mtok,
          is_active: initial.is_active, api_key: "",
        }
      : { ...EMPTY },
  );
  const [run, busy, error] = useAction();
  const set = <K extends keyof AIConnectionIn>(k: K, v: AIConnectionIn[K]) => setForm((f) => ({ ...f, [k]: v }));
  const effort = String(form.default_params?.effort ?? "");
  const needsUrl = form.provider === "openai_compatible" || form.provider === "azure_openai";

  async function save() {
    const body: Record<string, unknown> = { ...form, base_url: form.base_url || null };
    if (!form.api_key) delete body.api_key; // vacío = conservar la clave guardada
    const r = await run(() =>
      initial ? send(`/api/ai/connections/${initial.id}`, "PUT", body) : send("/api/ai/connections", "POST", body),
    );
    if (r !== undefined) onSaved();
  }

  return (
    <Modal
      title={initial ? `Conexión: ${initial.name}` : "Nueva conexión a un LLM"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !form.name.trim() || !form.model.trim() || (needsUrl && !form.base_url)} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <Field label="Nombre">
        <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Claude principal" />
      </Field>
      <div className="grid2">
        <Field label="Proveedor">
          <select value={form.provider} onChange={(e) => set("provider", e.target.value as AIProvider)}>
            {(Object.keys(PROVIDER_LABEL) as AIProvider[]).map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABEL[p]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Modelo">
          <input list="conn-models" value={form.model} onChange={(e) => set("model", e.target.value)} />
          <datalist id="conn-models">
            {MODEL_SUGGESTIONS[form.provider].map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
      </div>
      {needsUrl && (
        <Field label="URL base" hint={form.provider === "azure_openai" ? "https://<recurso>.openai.azure.com" : "https://…/v1 (Groq, vLLM, Ollama, Together…)"}>
          <input value={form.base_url ?? ""} onChange={(e) => set("base_url", e.target.value)} />
        </Field>
      )}
      <Field
        label="API key"
        hint={
          initial?.has_api_key
            ? "Clave guardada en Vault ✓ — deja vacío para conservarla"
            : "Vacío = usa la clave del servidor (ANTHROPIC_API_KEY / OPENAI_API_KEY). Se guarda cifrada en Supabase Vault."
        }
      >
        <input type="password" autoComplete="off" value={form.api_key ?? ""} onChange={(e) => set("api_key", e.target.value)} />
      </Field>
      {initial?.has_api_key && (
        <Toggle checked={Boolean(form.clear_api_key)} onChange={(v) => set("clear_api_key", v)} label="Quitar la clave guardada" />
      )}
      <div className="grid3">
        {form.provider === "anthropic" && (
          <Field label="Esfuerzo">
            <select value={effort} onChange={(e) => set("default_params", { ...form.default_params, effort: e.target.value || undefined })}>
              {["", "low", "medium", "high", "xhigh", "max"].map((x) => (
                <option key={x} value={x}>
                  {x || "por defecto"}
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field label="Timeout (ms)">
          <input type="number" min={1000} step={1000} value={form.timeout_ms} onChange={(e) => set("timeout_ms", Number(e.target.value))} />
        </Field>
        <Field label="USD / M tokens entrada">
          <input type="number" step="0.01" value={form.input_cost_per_mtok ?? ""} onChange={(e) => set("input_cost_per_mtok", e.target.value === "" ? null : Number(e.target.value))} />
        </Field>
        <Field label="USD / M tokens salida">
          <input type="number" step="0.01" value={form.output_cost_per_mtok ?? ""} onChange={(e) => set("output_cost_per_mtok", e.target.value === "" ? null : Number(e.target.value))} />
        </Field>
      </div>
      <Toggle checked={form.is_active} onChange={(v) => set("is_active", v)} label="Conexión activa" />
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function ConnectionsPanel({
  connections,
  health,
  isAdmin,
  reload,
}: {
  connections: AIConnection[];
  health: Record<number, ConnectionHealth>;
  isAdmin: boolean;
  reload: () => void;
}) {
  const [editing, setEditing] = useState<AIConnection | "new" | null>(null);
  const [tested, setTested] = useState<Record<number, string>>({});
  const [run, busy, error] = useAction();

  async function test(c: AIConnection) {
    const r = await run(() => send<CortexTestResult>(`/api/ai/connections/${c.id}/test`, "POST"));
    if (r) {
      const last = r.attempts?.[r.attempts.length - 1];
      setTested((t) => ({ ...t, [c.id]: r.ok ? `✓ ${last?.latency_ms ?? "?"} ms` : `✗ ${last?.error ?? r.error ?? "falló"}` }));
    }
    reload();
  }

  async function remove(c: AIConnection) {
    if (!confirm(`¿Eliminar la conexión «${c.name}»? Se quitará de los Cortex que la usan.`)) return;
    await run(() => send(`/api/ai/connections/${c.id}`, "DELETE"));
    reload();
  }

  return (
    <Card title="Conexiones a LLMs" actions={isAdmin ? <button className="primary" onClick={() => setEditing("new")}>Nueva conexión</button> : undefined}>
      <ErrorBox error={error} />
      {connections.length === 0 ? (
        <Empty>No hay conexiones. Crea una con tu proveedor (Claude, OpenAI o un endpoint compatible).</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Conexión</th>
                <th>Proveedor / modelo</th>
                <th>Clave</th>
                <th>Estado</th>
                <th className="num">p50 / p95</th>
                <th className="num">Timeout</th>
                <th>Último error</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {connections.map((c) => {
                const h = health[c.id] ?? c.health ?? null;
                return (
                  <tr key={c.id}>
                    <td>
                      <strong>{c.name}</strong>
                      {!c.is_active && <div className="small muted">Inactiva</div>}
                    </td>
                    <td>
                      {PROVIDER_LABEL[c.provider]}
                      <div className="small muted">{c.model}</div>
                      {c.base_url && <div className="small muted">{c.base_url}</div>}
                    </td>
                    <td>
                      {c.has_api_key ? <Badge tone="info">Vault</Badge> : c.uses_server_key === false
                        ? <Badge tone="bad">Sin clave</Badge> : <span className="small muted">Servidor</span>}
                    </td>
                    <td>
                      <HealthBadge h={h} />
                      {tested[c.id] && <div className="small">{tested[c.id]}</div>}
                    </td>
                    <td className="num">
                      {h?.latency_p50_ms ?? "—"} / {h?.latency_p95_ms ?? "—"} ms
                    </td>
                    <td className="num">{(c.timeout_ms / 1000).toLocaleString("es")} s</td>
                    <td className="small muted" style={{ maxWidth: 240 }}>
                      {h?.last_error ? `${h.last_error.slice(0, 120)} · ${timeAgo(h.last_failure_at)}` : "—"}
                    </td>
                    <td className="nowrap">
                      <button disabled={busy} onClick={() => test(c)}>Probar</button>{" "}
                      {isAdmin && (
                        <>
                          <button onClick={() => setEditing(c)}>Editar</button>{" "}
                          <button className="danger" onClick={() => remove(c)}>Eliminar</button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <ConnectionModal
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </Card>
  );
}
