"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, Tabs, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import { fmtDateTime, qs, timeAgo } from "@/lib/api";
import { TRIGGER_TYPES } from "@/lib/flow-catalog";
import type { EditorMode, Flow, FlowRun, FlowRunDetail, FlowStatus, InputValue } from "@/lib/flow-types";
import { flowsApi, saveVersion, useCatalog } from "./api";
import BlockInputsForm from "./BlockInputsForm";
import { emptyDefinition } from "./convert";

export const STATUS: Record<FlowStatus, { label: string; tone: "ok" | "warn" | "neutral" | "info" }> = {
  draft: { label: "Borrador", tone: "neutral" },
  active: { label: "Activo", tone: "ok" },
  paused: { label: "Pausado", tone: "warn" },
  archived: { label: "Archivado", tone: "neutral" },
};
const RUN_TONE: Record<FlowRun["status"], "ok" | "warn" | "bad" | "neutral" | "info"> = {
  running: "info",
  waiting: "warn",
  succeeded: "ok",
  failed: "bad",
  cancelled: "neutral",
};
const triggerLabel = (t: string) => TRIGGER_TYPES.find((x) => x.value === t)?.label ?? t;

function CreateFlowModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const catalog = useCatalog();
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState("inbound_message");
  const [config, setConfig] = useState<Record<string, InputValue>>({});
  const [mode, setMode] = useState<EditorMode>("junior");
  const [run, busy, error] = useAction();
  const hat = catalog.blocks.find((b) => b.type === trigger);

  async function create() {
    const flow = await run(async () => {
      const f = await flowsApi.create({ name: name.trim(), trigger_type: trigger as Flow["trigger_type"], trigger_config: config, editor_mode: mode });
      // Primera versión: un guion con el mismo evento de inicio
      const def = emptyDefinition(trigger);
      def.scripts[0].trigger.config = config;
      const detail = await flowsApi.get(f.id).catch(() => null);
      if (!detail?.definition) await saveVersion(f.id, def, "Versión inicial").catch(() => null);
      return f;
    });
    if (flow) router.push(`/automatizaciones/flujos/${flow.id}`);
  }

  return (
    <Modal
      title="Nuevo flujo"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !name.trim()} onClick={create}>
            Crear y abrir editor
          </button>
        </>
      }
    >
      <Field label="Nombre">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ej. Bienvenida y calificación" autoFocus />
      </Field>
      <Field label="¿Cuándo empieza?">
        <select
          value={trigger}
          onChange={(e) => {
            setTrigger(e.target.value);
            setConfig({});
          }}
        >
          {catalog.blocks
            .filter((b) => b.shape === "hat")
            .map((b) => (
              <option key={b.type} value={b.type}>
                {b.icon} {b.label}
              </option>
            ))}
        </select>
      </Field>
      {hat && hat.inputs.length > 0 && (
        <BlockInputsForm
          inputs={hat.inputs}
          values={config}
          onChange={(n, v) => setConfig((c) => ({ ...c, [n]: v }))}
        />
      )}
      <Field label="Editor">
        <div className="inline">
          <label className="inline">
            <input type="radio" checked={mode === "junior"} onChange={() => setMode("junior")} /> 🧸 Junior (bloques grandes con íconos)
          </label>
          <label className="inline">
            <input type="radio" checked={mode === "advanced"} onChange={() => setMode("advanced")} /> 🧩 Avanzado (estilo Scratch 3)
          </label>
        </div>
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

function RunDetailModal({ runId, onClose }: { runId: number; onClose: () => void }) {
  const { data, error, loading } = useApi<FlowRunDetail>(`/api/flows/runs/${runId}`);
  return (
    <Modal title={`Ejecución #${runId}`} onClose={onClose} wide>
      <ErrorBox error={error} />
      {loading && !data ? (
        <Loading />
      ) : data ? (
        <>
          <div className="inline">
            <Badge tone={RUN_TONE[data.status]}>{data.status}</Badge>
            <span className="small muted">
              {fmtDateTime(data.started_at)} → {fmtDateTime(data.finished_at)}
            </span>
            {data.conversation_id && <Link href={`/conversaciones?id=${data.conversation_id}`}>Ver conversación</Link>}
          </div>
          {data.error && <div className="error-box">{data.error}</div>}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Bloque</th>
                  <th>Estado</th>
                  <th>Salida</th>
                  <th className="num">ms</th>
                </tr>
              </thead>
              <tbody>
                {data.steps.map((s) => (
                  <tr key={s.id}>
                    <td>
                      {s.block_type} <span className="small muted">{s.block_id}</span>
                    </td>
                    <td>
                      <Badge tone={s.status === "ok" ? "ok" : s.status === "error" ? "bad" : s.status === "waiting" ? "warn" : "neutral"}>
                        {s.status}
                      </Badge>
                    </td>
                    <td className="small">
                      {s.error ? <span className="error">{s.error}</span> : <code>{JSON.stringify(s.output ?? null)?.slice(0, 160)}</code>}
                    </td>
                    <td className="num">{s.latency_ms ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </Modal>
  );
}

function RunsTab({ flows }: { flows: Flow[] }) {
  const [flowId, setFlowId] = useState<number | null>(flows[0]?.id ?? null);
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const runs = useApi<FlowRun[]>(flowId ? `/api/flows/${flowId}/runs${qs({ status })}` : null);
  return (
    <Card
      title="Ejecuciones"
      actions={
        <>
          <select value={flowId ?? ""} onChange={(e) => setFlowId(Number(e.target.value) || null)} aria-label="Flujo">
            {flows.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Estado">
            <option value="">Todas</option>
            <option value="running">En curso</option>
            <option value="waiting">Esperando</option>
            <option value="succeeded">Completadas</option>
            <option value="failed">Fallidas</option>
          </select>
          <button onClick={() => runs.reload()}>Actualizar</button>
        </>
      }
    >
      <ErrorBox error={runs.error} />
      {!flowId ? (
        <Empty>Crea un flujo para ver sus ejecuciones.</Empty>
      ) : runs.loading && !runs.data ? (
        <Loading />
      ) : !runs.data?.length ? (
        <Empty>Este flujo aún no se ha ejecutado.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Estado</th>
                <th>Inicio</th>
                <th>Evento</th>
                <th>Conversación</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => setOpen(r.id)}>
                  <td>{r.id}</td>
                  <td>
                    <Badge tone={RUN_TONE[r.status]}>{r.status}</Badge>
                    {r.status === "waiting" && r.resume_at && <div className="small muted">hasta {fmtDateTime(r.resume_at)}</div>}
                  </td>
                  <td title={fmtDateTime(r.started_at)}>{timeAgo(r.started_at)}</td>
                  <td>{triggerLabel(r.trigger_type)}</td>
                  <td>{r.conversation_id ? `#${r.conversation_id}` : "—"}</td>
                  <td className="small error">{r.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <RunDetailModal runId={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}

export default function FlowList() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const flows = useApi<Flow[]>("/api/flows");
  const [tab, setTab] = useState<"flows" | "runs">("flows");
  const [creating, setCreating] = useState(false);
  const [run, busy, error] = useAction();

  async function act(f: Flow, action: "publish" | "pause" | "archive") {
    await run(async () => {
      if (action === "publish") {
        const versions = await flowsApi.versions(f.id);
        const latest = versions.reduce<null | (typeof versions)[number]>((a, v) => (!a || v.version > a.version ? v : a), null);
        if (!latest) throw new Error("Guarda una versión en el editor antes de publicar");
        await flowsApi.publish(f.id, latest.id);
      } else if (action === "pause") await flowsApi.pause(f.id);
      else if (!confirm(`¿Archivar «${f.name}»? Dejará de ejecutarse.`)) return;
      else await flowsApi.archive(f.id);
      await flows.reload();
    });
  }

  return (
    <>
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          ["flows", "Flujos"],
          ["runs", "Ejecuciones"],
        ]}
      />
      <ErrorBox error={error || flows.error} />
      {tab === "runs" ? (
        <RunsTab flows={flows.data ?? []} />
      ) : (
        <Card
          title="Flujos"
          actions={
            isAdmin && (
              <button className="primary" onClick={() => setCreating(true)}>
                + Nuevo flujo
              </button>
            )
          }
        >
          {flows.loading && !flows.data ? (
            <Loading />
          ) : !flows.data?.length ? (
            <Empty>
              Aún no hay flujos. Crea uno con bloques tipo Scratch: un evento de inicio y los pasos que siguen.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Flujo</th>
                    <th>Estado</th>
                    <th>Inicio</th>
                    <th>Versión</th>
                    <th>Editor</th>
                    <th>Actualizado</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {flows.data.map((f) => (
                    <tr key={f.id}>
                      <td>
                        <Link href={`/automatizaciones/flujos/${f.id}`} className="strong">
                          {f.name}
                        </Link>
                        {f.description && <div className="small muted">{f.description}</div>}
                      </td>
                      <td>
                        <Badge tone={STATUS[f.status].tone}>{STATUS[f.status].label}</Badge>
                      </td>
                      <td>{triggerLabel(f.trigger_type)}</td>
                      <td>{f.current_version ? `v${f.current_version.version}` : <span className="muted">sin publicar</span>}</td>
                      <td>{f.editor_mode === "junior" ? "🧸 Junior" : "🧩 Avanzado"}</td>
                      <td title={fmtDateTime(f.updated_at)}>{timeAgo(f.updated_at)}</td>
                      <td className="nowrap">
                        <Link href={`/automatizaciones/flujos/${f.id}`}>
                          <button>Editar</button>
                        </Link>{" "}
                        {isAdmin && f.status !== "active" && f.status !== "archived" && (
                          <button disabled={busy} onClick={() => act(f, "publish")}>
                            Publicar
                          </button>
                        )}{" "}
                        {isAdmin && f.status === "active" && (
                          <button disabled={busy} onClick={() => act(f, "pause")}>
                            Pausar
                          </button>
                        )}{" "}
                        {isAdmin && f.status !== "archived" && (
                          <button className="danger" disabled={busy} onClick={() => act(f, "archive")}>
                            Archivar
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
      {creating && <CreateFlowModal onClose={() => setCreating(false)} />}
    </>
  );
}
