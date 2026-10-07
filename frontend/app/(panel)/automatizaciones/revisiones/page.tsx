"use client";

import { useState } from "react";
import { api, fmtDateTime, qs, send } from "@/lib/api";
import type { AIAgent, Cortex, DiffRow, Revision } from "@/lib/ai-types";
import { jsonDiff } from "@/components/ai/jsonDiff";
import { Badge, Card, Empty, ErrorBox, Field, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { useIsAdmin } from "@/components/config/common";
import { DiffTable } from "@/components/ai/JsonEditWithAI";

const ENTITY_LABEL: Record<string, string> = {
  ai_agent: "Agente de IA",
  cortex: "Cortex",
  flow: "Flujo",
  automation: "Automatización",
  classifier: "Clasificador",
  setting: "Configuración",
};
const SETTINGS = ["company", "conversations", "classifier", "appointments", "catalog", "learning", "reports"];
const SOURCE: Record<string, [string, "info" | "ok" | "neutral" | "warn"]> = {
  ai: ["✨ IA", "info"],
  human: ["👤 Persona", "ok"],
  import: ["📄 Importación", "warn"],
  system: ["Sistema", "neutral"],
};

/** Diff plano por rutas (a.b[0].c) entre dos documentos JSON. */

/** Opciones del selector de entidad según el tipo (con texto libre para flujos y automatizaciones). */
function useEntityOptions(type: string): { value: string; label: string }[] | null {
  const agents = useApi<AIAgent[]>(type === "ai_agent" ? "/api/bots" : null);
  const cortexes = useApi<Cortex[]>(type === "cortex" ? "/api/ai/cortexes" : null);
  const automations = useApi<{ id: number; name: string }[]>(type === "automation" ? "/api/automations" : null);
  if (type === "ai_agent") return (agents.data ?? []).map((a) => ({ value: String(a.id), label: a.name }));
  if (type === "cortex") return (cortexes.data ?? []).map((c) => ({ value: String(c.id), label: c.name }));
  if (type === "automation") return (automations.data ?? []).map((a) => ({ value: String(a.id), label: a.name }));
  if (type === "setting") return SETTINGS.map((k) => ({ value: k, label: k }));
  if (type === "classifier") return [{ value: "classifier", label: "classifier" }];
  return null; // flujo: id libre
}

export default function RevisionsPage() {
  const isAdmin = useIsAdmin();
  const [entityType, setEntityType] = useState("ai_agent");
  const [entityId, setEntityId] = useState("");
  const options = useEntityOptions(entityType);
  const revs = useApi<Revision[]>(entityId ? `/api/revisions${qs({ entity_type: entityType, entity_id: entityId })}` : null);
  const [picked, setPicked] = useState<number[]>([]);
  const [view, setView] = useState<{ title: string; doc?: unknown; diff?: DiffRow[] } | null>(null);
  const [run, busy, error] = useAction();
  const rows = entityId ? revs.data ?? [] : [];

  const load = (id: number) => api<Revision>(`/api/revisions/${id}`);
  async function show(r: Revision) {
    const full = await run(() => load(r.id));
    if (full) setView({ title: `${ENTITY_LABEL[entityType]} ${r.entity_id} · revisión ${r.revision}`, doc: full.document });
  }
  async function compare() {
    const [x, y] = [...picked].sort((a, b) => a - b);
    const res = await run(() => Promise.all([load(x), load(y)]));
    if (res) {
      const [a, b] = res.sort((m, n) => m.revision - n.revision);
      setView({ title: `Revisión ${a.revision} → ${b.revision}`, diff: jsonDiff(a.document, b.document) });
    }
  }
  async function restore(r: Revision) {
    if (!confirm(`¿Restaurar la revisión ${r.revision}? Se guardará como una revisión nueva.`)) return;
    if ((await run(() => send(`/api/revisions/${r.id}/restore`, "POST"))) !== undefined) revs.reload();
  }
  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const changeType = (t: string) => {
    setEntityType(t);
    setEntityId(t === "classifier" ? "classifier" : "");
    setPicked([]);
  };

  return (
    <>
      <PageHeader
        title="Historial JSON"
        subtitle="Cada cambio en agentes, Cortex, flujos, automatizaciones y configuración queda versionado, indicando si lo hizo una persona o la IA (y con qué instrucción)."
      />
      <Card title="Entidad">
        <div className="grid2">
          <Field label="Tipo">
            <select value={entityType} onChange={(e) => changeType(e.target.value)}>
              {Object.entries(ENTITY_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Field>
          <Field label={options ? "Elemento" : "ID"}>
            {options ? (
              <select value={entityId} onChange={(e) => { setEntityId(e.target.value); setPicked([]); }}>
                <option value="">Selecciona…</option>
                {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            ) : (
              <input value={entityId} placeholder="ID del flujo" onChange={(e) => { setEntityId(e.target.value.trim()); setPicked([]); }} />
            )}
          </Field>
        </div>
      </Card>
      <Card
        title="Revisiones"
        actions={<button disabled={picked.length !== 2 || busy} onClick={compare}>Comparar 2 seleccionadas</button>}
      >
        <ErrorBox error={revs.error || error} />
        {!entityId ? (
          <Empty>Elige una entidad para ver su historial.</Empty>
        ) : rows.length === 0 ? (
          <Empty>{revs.loading ? "Cargando…" : "Sin revisiones para esta entidad."}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th /><th>Fecha</th><th className="num">Rev.</th><th>Origen</th><th>Instrucción a la IA</th><th /></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td><input type="checkbox" checked={picked.includes(r.id)} onChange={() => toggle(r.id)} /></td>
                    <td className="small nowrap">{fmtDateTime(r.created_at)}</td>
                    <td className="num">{r.revision}</td>
                    <td><Badge tone={SOURCE[r.source]?.[1] ?? "neutral"}>{SOURCE[r.source]?.[0] ?? r.source}</Badge></td>
                    <td className="small" style={{ maxWidth: 380 }}>{r.ai_prompt ?? <span className="muted">—</span>}</td>
                    <td className="nowrap">
                      <button disabled={busy} onClick={() => show(r)}>Ver JSON</button>{" "}
                      {isAdmin && <button disabled={busy} onClick={() => restore(r)}>Restaurar</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="small muted">Selecciona dos revisiones para ver qué cambió entre ellas.</p>
      </Card>
      {view && (
        <Modal wide title={view.title} onClose={() => setView(null)}>
          {view.diff ? <DiffTable diff={view.diff} /> : (
            <pre className="small" style={{ background: "var(--panel-2)", padding: 12, borderRadius: 8, maxHeight: 520, overflow: "auto" }}>
              {JSON.stringify(view.doc, null, 2)}
            </pre>
          )}
        </Modal>
      )}
    </>
  );
}
