"use client";

import { useState } from "react";
import { daysAgo, isoDay, qs, send, timeAgo } from "@/lib/api";
import {
  MEMORY_KIND_LABEL,
  type Cortex,
  type LearningRun,
  type MemoryItem,
  type MemoryKind,
  type MemoryList,
  type MemoryStatus,
} from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, Field, Modal, PageHeader, Tabs, useAction, useApi } from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";
import RunsList from "@/components/ai/RunsList";

const KINDS = Object.keys(MEMORY_KIND_LABEL) as MemoryKind[];


function ItemCard({ m, isAdmin, onChanged }: { m: MemoryItem; isAdmin: boolean; onChanged: () => void }) {
  const [edit, setEdit] = useState(false);
  const [form, setForm] = useState({ kind: m.kind, title: m.title, content: m.content });
  const [run, busy, error] = useAction();
  const act = async (fn: () => Promise<unknown>) => {
    if ((await run(fn)) !== undefined) {
      setEdit(false);
      onChanged();
    }
  };

  return (
    <div className="card" style={{ marginTop: 0 }}>
      {edit ? (
        <div className="form">
          <div className="grid2">
            <Field label="Tipo">
              <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as MemoryKind })}>
                {KINDS.map((k) => <option key={k} value={k}>{MEMORY_KIND_LABEL[k]}</option>)}
              </select>
            </Field>
            <Field label="Título">
              <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            </Field>
          </div>
          <Field label="Contenido (listo para que lo use el agente)">
            <textarea rows={4} value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
          </Field>
          <div className="inline">
            <button className="primary" disabled={busy} onClick={() => act(() => send(`/api/memory/items/${m.id}`, "PUT", form))}>Guardar</button>
            <button onClick={() => setEdit(false)}>Cancelar</button>
          </div>
        </div>
      ) : (
        <>
          <div className="row">
            <div>
              <Badge tone="info">{MEMORY_KIND_LABEL[m.kind]}</Badge> <strong>{m.title}</strong>
            </div>
            <span className="small muted nowrap">
              {m.source === "manual" ? "Manual" : `Aprendido${m.confidence != null ? ` · ${Math.round(m.confidence * 100)} %` : ""}`} ·{" "}
              {timeAgo(m.created_at)}
            </span>
          </div>
          <p style={{ whiteSpace: "pre-wrap", margin: "6px 0" }}>{m.content}</p>
          {m.evidence_conversation_ids.length > 0 && (
            <div className="small muted">
              Evidencia:{" "}
              {m.evidence_conversation_ids.slice(0, 8).map((id, i) => (
                <span key={id}>
                  {i > 0 && ", "}
                  <a href={`/conversaciones?id=${id}`}>conv. {id}</a>
                </span>
              ))}
            </div>
          )}
          {isAdmin && (
            <div className="inline" style={{ marginTop: 8 }}>
              {m.status !== "approved" && (
                <button className="primary" disabled={busy} onClick={() => act(() => send(`/api/memory/items/${m.id}/approve`, "POST"))}>Aprobar</button>
              )}
              {m.status !== "rejected" && (
                <button disabled={busy} onClick={() => act(() => send(`/api/memory/items/${m.id}/reject`, "POST"))}>Rechazar</button>
              )}
              <button disabled={busy} onClick={() => setEdit(true)}>Editar</button>
            </div>
          )}
        </>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

function GenerateModal({ onClose, onStarted }: { onClose: () => void; onStarted: () => void }) {
  const typs = useApi<{ typifications: string[] }>("/api/settings/conversations");
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes");
  const [f, setF] = useState({ start: daysAgo(90), end: isoDay(), typifications: ["Venta"] as string[], max_conversations: 60, cortex_id: "" });
  const [run, busy, error] = useAction();
  const toggle = (t: string) =>
    setF((x) => ({ ...x, typifications: x.typifications.includes(t) ? x.typifications.filter((y) => y !== t) : [...x.typifications, t] }));

  async function start() {
    const r = await run(() => send("/api/memory/runs", "POST", { ...f, cortex_id: f.cortex_id ? Number(f.cortex_id) : null }));
    if (r !== undefined) onStarted();
  }

  return (
    <Modal
      title="Generar memoria desde conversaciones"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy} onClick={start}>Generar</button>
        </>
      }
    >
      <p className="muted small" style={{ margin: 0 }}>
        La IA lee conversaciones cerradas y propone FAQs, objeciones, respuestas ganadoras, datos y políticas. Nada se usa
        hasta que un administrador lo aprueba.
      </p>
      <div className="grid2">
        <Field label="Desde"><input type="date" value={f.start} onChange={(e) => setF({ ...f, start: e.target.value })} /></Field>
        <Field label="Hasta"><input type="date" value={f.end} onChange={(e) => setF({ ...f, end: e.target.value })} /></Field>
      </div>
      <Field label="Tipificaciones a analizar" hint="Vacío = todas las conversaciones cerradas.">
        <div className="chips">
          {(typs.data?.typifications ?? []).map((t) => (
            <button key={t} type="button" className={f.typifications.includes(t) ? "chip active" : "chip"} onClick={() => toggle(t)}>{t}</button>
          ))}
        </div>
      </Field>
      <div className="grid2">
        <Field label="Máximo de conversaciones">
          <input type="number" min={1} max={500} value={f.max_conversations} onChange={(e) => setF({ ...f, max_conversations: Number(e.target.value) })} />
        </Field>
        <Field label="Cortex">
          <select value={f.cortex_id} onChange={(e) => setF({ ...f, cortex_id: e.target.value })}>
            <option value="">Automático (aprendizaje)</option>
            {(cortexes.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
      </div>
      <ErrorBox error={error} />
    </Modal>
  );
}

function ManualModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ kind: "faq" as MemoryKind, title: "", content: "" });
  const [run, busy, error] = useAction();
  return (
    <Modal
      title="Agregar a la memoria"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !f.title.trim() || !f.content.trim()}
            onClick={async () => (await run(() => send("/api/memory/items", "POST", f))) !== undefined && onSaved()}>
            Guardar (aprobado)
          </button>
        </>
      }
    >
      <Field label="Tipo">
        <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as MemoryKind })}>
          {KINDS.map((k) => <option key={k} value={k}>{MEMORY_KIND_LABEL[k]}</option>)}
        </select>
      </Field>
      <Field label="Título"><input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
      <Field label="Contenido"><textarea rows={5} value={f.content} onChange={(e) => setF({ ...f, content: e.target.value })} /></Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function MemoryPage() {
  const isAdmin = useIsAdmin();
  const [status, setStatus] = useState<MemoryStatus>("pending");
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");
  const [modal, setModal] = useState<"generate" | "manual" | null>(null);
  const list = useApi<MemoryList>(`/api/memory/items${qs({ status, kind, q })}`);
  const runs = useApi<LearningRun[]>("/api/memory/runs");
  const rows = list.data?.items ?? [];
  const counts = list.data?.counts ?? {};

  return (
    <>
      <PageHeader
        title="Memoria del negocio"
        subtitle="Conocimiento aprendido de conversaciones reales (o escrito a mano) que usan los agentes con «Memoria del negocio» activa. Solo se usa lo aprobado."
        actions={
          isAdmin ? (
            <>
              <button onClick={() => setModal("manual")}>Agregar manual</button>
              <button className="primary" onClick={() => setModal("generate")}>✨ Generar memoria</button>
            </>
          ) : undefined
        }
      />
      <AdminNotice />
      <Tabs value={status} onChange={setStatus} tabs={[
        ["pending", `Pendientes (${counts.pending ?? 0})`],
        ["approved", `Aprobadas (${counts.approved ?? 0})`],
        ["rejected", `Rechazadas (${counts.rejected ?? 0})`],
      ]} />
      <div className="inline" style={{ marginBottom: 12 }}>
        <input style={{ maxWidth: 280 }} placeholder="Buscar" value={q} onChange={(e) => setQ(e.target.value)} />
        <select style={{ width: "auto" }} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Todos los tipos</option>
          {KINDS.map((k) => <option key={k} value={k}>{MEMORY_KIND_LABEL[k]}</option>)}
        </select>
        <span className="muted small">{rows.length} elemento(s)</span>
      </div>
      <ErrorBox error={list.error} />
      {rows.length === 0 ? (
        <Card><Empty>{list.loading ? "Cargando…" : status === "pending" ? "No hay propuestas pendientes. Genera memoria desde las conversaciones." : "Sin elementos."}</Empty></Card>
      ) : (
        <div className="stack">
          {rows.map((m) => <ItemCard key={m.id} m={m} isAdmin={isAdmin} onChanged={list.reload} />)}
        </div>
      )}
      <div style={{ marginTop: 16 }}>
        <RunsList runs={runs.data ?? []} reload={() => { runs.reload(); list.reload(); }} title="Generaciones de memoria" />
      </div>
      {modal === "generate" && (
        <GenerateModal onClose={() => setModal(null)} onStarted={() => { setModal(null); runs.reload(); }} />
      )}
      {modal === "manual" && (
        <ManualModal onClose={() => setModal(null)} onSaved={() => { setModal(null); setStatus("approved"); list.reload(); }} />
      )}
    </>
  );
}
