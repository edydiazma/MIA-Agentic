"use client";

import { useRef, useState } from "react";
import { api, qs, send } from "@/lib/api";
import type { Cortex, DiffRow, JsonDocument, JsonEditProposal, JsonEntityType } from "@/lib/ai-types";
import { ErrorBox, Field, Modal, useAction, useApi } from "@/components/ui";

const show = (v: unknown) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

export function DiffTable({ diff }: { diff: DiffRow[] }) {
  if (!diff.length) return <p className="muted">Sin cambios respecto a la versión actual.</p>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Ruta</th>
            <th>Antes</th>
            <th>Después</th>
          </tr>
        </thead>
        <tbody>
          {diff.map((d) => (
            <tr key={d.path}>
              <td className="nowrap small strong">{d.path}</td>
              <td>
                <pre className="small" style={{ margin: 0, whiteSpace: "pre-wrap", color: "var(--bad)" }}>{show(d.before)}</pre>
              </td>
              <td>
                <pre className="small" style={{ margin: 0, whiteSpace: "pre-wrap", color: "var(--ok)" }}>{show(d.after)}</pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Edición de cualquier entidad JSON con IA ("n8n-style"): instrucción en lenguaje natural → propuesta
 * validada por el backend → diff → aplicar (queda como revisión con fuente IA). También exporta/importa el JSON.
 */
export default function JsonEditWithAI({
  entityType,
  entityId,
  title,
  onClose,
  onApplied,
}: {
  entityType: JsonEntityType;
  entityId: string | number;
  title?: string;
  onClose: () => void;
  onApplied?: () => void;
}) {
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes");
  const [instruction, setInstruction] = useState("");
  const [cortexId, setCortexId] = useState<string>("");
  const [proposal, setProposal] = useState<JsonEditProposal | null>(null);
  const [raw, setRaw] = useState(false);
  const [imported, setImported] = useState<string | null>(null);
  const [run, busy, error] = useAction();
  const file = useRef<HTMLInputElement>(null);
  const ref = { entity_type: entityType, entity_id: String(entityId) };

  async function propose() {
    const r = await run(() =>
      send<JsonEditProposal>("/api/ai/json-edit", "POST", {
        ...ref,
        instruction,
        cortex_id: cortexId ? Number(cortexId) : null,
      }),
    );
    if (r) setProposal(r);
  }

  async function apply(document: unknown, note: string) {
    const r = await run(() => send("/api/ai/json-edit/apply", "POST", { ...ref, document, instruction: note }));
    if (r !== undefined) {
      onApplied?.();
      onClose();
    }
  }

  async function exportJson() {
    const doc = await run(() => api<JsonDocument>(`/api/ai/json-edit/document${qs(ref)}`));
    if (doc === undefined) return;
    const blob = new Blob([JSON.stringify(doc.document, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${entityType}-${entityId}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function importJson(text: string) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setImported(null);
      return run(async () => {
        throw new Error("El archivo no es JSON válido");
      });
    }
    const r = await run(() => send("/api/ai/json-edit/document", "PUT", { ...ref, document: parsed }));
    if (r !== undefined) {
      onApplied?.();
      onClose();
    }
  }

  return (
    <Modal
      title={title ?? "Editar con IA"}
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={exportJson} disabled={busy}>⬇ Exportar JSON</button>
          <button onClick={() => file.current?.click()} disabled={busy}>⬆ Importar JSON</button>
          <input
            ref={file}
            type="file"
            accept="application/json,.json"
            hidden
            onChange={async (e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) {
                const text = await f.text();
                setImported(text);
              }
            }}
          />
          <span style={{ flex: 1 }} />
          <button onClick={onClose}>Cancelar</button>
          {proposal ? (
            <button className="primary" disabled={busy || !proposal.diff.length || !proposal.valid} onClick={() => apply(proposal.proposal, instruction)}>
              Aplicar cambios
            </button>
          ) : (
            <button className="primary" disabled={busy || !instruction.trim()} onClick={propose}>
              {busy ? "Pensando…" : "✨ Proponer cambios"}
            </button>
          )}
        </>
      }
    >
      <p className="muted small" style={{ margin: 0 }}>
        Describe el cambio en lenguaje natural. La IA devuelve el documento completo, el sistema lo valida contra su
        esquema y te muestra las diferencias antes de aplicar. Cada cambio queda en el historial JSON.
      </p>
      <Field label="Instrucción">
        <textarea
          rows={3}
          value={instruction}
          placeholder="Ej.: agrega la regla de no ofrecer descuentos mayores al 5 % y sube el tiempo máximo a 8 s"
          onChange={(e) => {
            setInstruction(e.target.value);
            setProposal(null);
          }}
        />
      </Field>
      <Field label="Cortex" hint="Vacío = el Cortex configurado para edición JSON (o el principal).">
        <select value={cortexId} onChange={(e) => setCortexId(e.target.value)}>
          <option value="">Automático</option>
          {(cortexes.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </Field>
      <ErrorBox error={error} />
      {imported !== null && (
        <div className="card">
          <p className="small" style={{ marginTop: 0 }}>
            Se reemplazará el documento actual por el archivo importado (se valida antes de guardar).
          </p>
          <div className="inline">
            <button className="primary" disabled={busy} onClick={() => importJson(imported)}>Importar</button>
            <button onClick={() => setImported(null)}>Cancelar importación</button>
          </div>
        </div>
      )}
      {proposal && (
        <>
          {proposal.summary && <p style={{ margin: 0 }}>{proposal.summary}</p>}
          {!proposal.valid && (
            <div className="error-box">
              La propuesta no cumple el esquema y no se puede aplicar:
              <ul style={{ margin: "4px 0 0" }}>{proposal.errors.map((e) => <li key={e}>{e}</li>)}</ul>
            </div>
          )}
          <div className="row">
            <strong>{proposal.diff.length} cambio(s) propuestos</strong>
            <button className="link" onClick={() => setRaw(!raw)}>{raw ? "Ver diferencias" : "Ver JSON completo"}</button>
          </div>
          {raw ? (
            <pre className="small" style={{ maxHeight: 360, overflow: "auto", background: "var(--panel-2)", padding: 12, borderRadius: 8 }}>
              {JSON.stringify(proposal.proposal, null, 2)}
            </pre>
          ) : (
            <DiffTable diff={proposal.diff} />
          )}
        </>
      )}
    </Modal>
  );
}
