"use client";

import { useState } from "react";
import { API_URL, fmtDateTime, getToken, send, timeAgo } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";
import type { DataExport, DatasetInfo, ExportRun } from "@/components/hub/types";

type Catalog = { datasets: DatasetInfo[]; parquet: boolean; destinations: string[]; schedules: string[] };

const DEST_LABEL: Record<string, string> = {
  download: "Descarga (archivo)",
  s3: "Amazon S3 (o compatible)",
  gcs: "Google Cloud Storage",
  bigquery: "BigQuery",
  sftp: "SFTP",
};
const SCHEDULE_LABEL: Record<string, string> = { hourly: "Cada hora", daily: "Diaria", weekly: "Semanal", manual: "Manual" };
const DATASET_LABEL: Record<string, string> = {
  contacts: "Clientes",
  conversations: "Conversaciones",
  messages: "Mensajes",
  deals: "Negocios",
  attributions: "Atribución",
  appointments: "Citas",
  orders: "Pedidos de tiendas",
  interaction_products: "Productos por interacción",
  products: "Catálogo",
  campaigns: "Campañas",
};
const CONFIG_FIELDS: Record<string, { key: string; label: string; secret?: boolean; textarea?: boolean }[]> = {
  download: [],
  s3: [
    { key: "bucket", label: "Bucket" },
    { key: "region", label: "Región (us-east-1)" },
    { key: "prefix", label: "Prefijo (carpeta)" },
    { key: "endpoint", label: "Endpoint compatible (opcional, https://…)" },
    { key: "access_key_id", label: "Access key ID", secret: true },
    { key: "secret_access_key", label: "Secret access key", secret: true },
  ],
  gcs: [
    { key: "bucket", label: "Bucket" },
    { key: "prefix", label: "Prefijo (carpeta)" },
    { key: "service_account", label: "Cuenta de servicio (JSON)", secret: true, textarea: true },
  ],
  bigquery: [
    { key: "project_id", label: "Proyecto" },
    { key: "dataset", label: "Dataset" },
    { key: "location", label: "Ubicación (US, EU, southamerica-east1…)" },
    { key: "table_prefix", label: "Prefijo de tablas (wa_)" },
    { key: "service_account", label: "Cuenta de servicio (JSON)", secret: true, textarea: true },
  ],
  sftp: [],
};

export default function ExportacionPage() {
  const catalog = useApi<Catalog>("/api/hub/exports/datasets");
  const list = useApi<DataExport[]>("/api/hub/exports");
  const [editing, setEditing] = useState<DataExport | "new" | null>(null);

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Exportación de datos: envía tus datos a tu almacén (BigQuery, S3, GCS) o descárgalos, de forma incremental y programada."
        actions={<button className="primary" onClick={() => setEditing("new")}>Nueva exportación</button>}
      />
      <ConfigTabs />
      <ErrorBox error={catalog.error ?? list.error} />
      {!catalog.data || !list.data ? (
        <Loading />
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          {!catalog.data.parquet && (
            <div className="small muted">Parquet no está disponible en este servidor: las exportaciones en parquet se generan en CSV.</div>
          )}
          {list.data.length === 0 ? (
            <Empty>Aún no hay exportaciones.</Empty>
          ) : (
            list.data.map((e) => <ExportCard key={e.id} exp={e} onEdit={() => setEditing(e)} reload={list.reload} />)
          )}
          <Card title="Looker Studio">
            <p className="small" style={{ margin: 0 }}>
              Con BigQuery como destino: en Looker Studio → Crear → Fuente de datos → BigQuery → elige el proyecto y el
              dataset, y usa las tablas <code>wa_*</code> (una por conjunto). Las corridas incrementales agregan filas; para
              el estado actual de cada registro, crea una vista que tome la fila más reciente por <code>id</code> (según{" "}
              <code>updated_at</code>).
            </p>
          </Card>
        </div>
      )}
      {editing && catalog.data && (
        <ExportModal
          exp={editing === "new" ? undefined : editing}
          catalog={catalog.data}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            list.reload();
          }}
        />
      )}
    </>
  );
}

function ExportCard({ exp, onEdit, reload }: { exp: DataExport; onEdit: () => void; reload: () => void }) {
  const [run, busy, error] = useAction();
  const [showRuns, setShowRuns] = useState(false);
  async function runNow(full = false) {
    if (await run(() => send(`/api/hub/exports/${exp.id}/run?full=${full}`, "POST"))) reload();
  }
  async function remove() {
    if (!confirm(`¿Eliminar la exportación ${exp.name}?`)) return;
    if (await run(() => send(`/api/hub/exports/${exp.id}`, "DELETE"))) reload();
  }
  return (
    <Card
      title={
        <span className="inline">
          {exp.name} <Badge tone={exp.is_active ? "ok" : "neutral"}>{exp.is_active ? "Activa" : "Pausada"}</Badge>
          {exp.last_status && <Badge tone={exp.last_status === "succeeded" ? "ok" : "bad"}>{exp.last_status === "succeeded" ? "Última OK" : "Última falló"}</Badge>}
        </span>
      }
      actions={
        <>
          <button disabled={busy} onClick={() => runNow()}>
            {busy ? "Exportando…" : "Exportar ahora"}
          </button>
          <button onClick={onEdit}>Editar</button>
          <button className="danger" disabled={busy} onClick={remove}>
            Eliminar
          </button>
        </>
      }
    >
      <div className="small muted">
        {DEST_LABEL[exp.destination]} · {SCHEDULE_LABEL[exp.schedule]} · {exp.effective_format.toUpperCase()} ·{" "}
        {exp.incremental ? "incremental" : "completa"} · {exp.datasets.map((d) => DATASET_LABEL[d] ?? d).join(", ")}
        {exp.include_sensitive && " · incluye datos sensibles"}
      </div>
      <div className="small" style={{ marginTop: 4 }}>
        Última exportación: <span title={fmtDateTime(exp.last_export_at)}>{timeAgo(exp.last_export_at)}</span>
      </div>
      {exp.last_error && <div className="error-box">{exp.last_error}</div>}
      <div className="inline">
        <button className="link small" onClick={() => setShowRuns(!showRuns)}>
          {showRuns ? "Ocultar corridas" : "Corridas"}
        </button>
        {exp.incremental && (
          <button className="link small" disabled={busy} onClick={() => runNow(true)}>
            Exportar todo de nuevo
          </button>
        )}
      </div>
      {showRuns && <Runs exp={exp} />}
      <ErrorBox error={error} />
    </Card>
  );
}

function Runs({ exp }: { exp: DataExport }) {
  const runs = useApi<ExportRun[]>(`/api/hub/exports/${exp.id}/runs?limit=20`);
  const [run, , error] = useAction();
  async function download(r: ExportRun, i: number) {
    await run(async () => {
      const res = await fetch(`${API_URL}/api/hub/exports/${exp.id}/runs/${r.id}/files/${i}`, {
        headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      });
      if (!res.ok) throw new Error("No se pudo descargar el archivo");
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = r.files[i].target?.split("/").pop() ?? "export";
      a.click();
      URL.revokeObjectURL(url);
      return true;
    });
  }
  if (!runs.data) return <Loading />;
  if (!runs.data.length) return <Empty>Aún no hay corridas.</Empty>;
  return (
    <>
      <table className="table">
        <thead>
          <tr>
            <th>Inicio</th>
            <th>Estado</th>
            <th>Filas</th>
            <th>Archivos</th>
          </tr>
        </thead>
        <tbody>
          {runs.data.map((r) => (
            <tr key={r.id} title={r.error ?? undefined}>
              <td className="small">{fmtDateTime(r.started_at)}</td>
              <td>
                <Badge tone={r.status === "succeeded" ? "ok" : r.status === "running" ? "info" : "bad"}>{r.status}</Badge>
              </td>
              <td>{r.rows_exported.toLocaleString("es")}</td>
              <td className="small">
                {r.files
                  .filter((f) => f.rows > 0)
                  .map((f, i) =>
                    exp.destination === "download" ? (
                      <button key={i} className="link small" onClick={() => download(r, r.files.indexOf(f))}>
                        {DATASET_LABEL[f.dataset] ?? f.dataset} ({f.rows})
                      </button>
                    ) : (
                      <div key={i}>
                        {DATASET_LABEL[f.dataset] ?? f.dataset}: {f.rows} → <code>{f.target}</code>
                      </div>
                    ),
                  )}
                {r.error && <div style={{ color: "var(--bad)" }}>{r.error}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorBox error={error} />
    </>
  );
}

function ExportModal({ exp, catalog, onClose, onSaved }: { exp?: DataExport; catalog: Catalog; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(exp?.name ?? "");
  const [destination, setDestination] = useState<string>(exp?.destination ?? "download");
  const [config, setConfig] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(exp?.config ?? {}).filter(([k]) => k !== "secrets_set").map(([k, v]) => [k, String(v ?? "")])),
  );
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [datasets, setDatasets] = useState<string[]>(exp?.datasets ?? ["contacts", "conversations", "deals", "attributions"]);
  const [format, setFormat] = useState<string>(exp?.format ?? (catalog.parquet ? "parquet" : "csv"));
  const [schedule, setSchedule] = useState<string>(exp?.schedule ?? "daily");
  const [incremental, setIncremental] = useState(exp?.incremental ?? true);
  const [sensitive, setSensitive] = useState(exp?.include_sensitive ?? false);
  const [active, setActive] = useState(exp?.is_active ?? true);
  const [run, busy, error] = useAction();
  const fields = CONFIG_FIELDS[destination] ?? [];
  const setSet = exp?.config.secrets_set ?? [];

  async function save() {
    const cfg: Record<string, string> = {};
    for (const f of fields) if (!f.secret && config[f.key]) cfg[f.key] = config[f.key];
    const body = {
      name,
      destination,
      config: cfg,
      secrets,
      datasets,
      format,
      schedule,
      incremental,
      include_sensitive: sensitive,
      is_active: active,
    };
    const r = await run(() => (exp ? send(`/api/hub/exports/${exp.id}`, "PUT", body) : send("/api/hub/exports", "POST", body)));
    if (r) onSaved();
  }

  return (
    <Modal
      wide
      title={exp ? `Editar ${exp.name}` : "Nueva exportación"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !name || !datasets.length} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <div className="form">
        <div className="grid2">
          <Field label="Nombre">
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Destino">
            <select value={destination} onChange={(e) => setDestination(e.target.value)}>
              {catalog.destinations
                .filter((d) => d !== "sftp")
                .map((d) => (
                  <option key={d} value={d}>
                    {DEST_LABEL[d] ?? d}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        {fields.map((f) =>
          f.secret ? (
            <Field key={f.key} label={f.label} hint={setSet.includes(f.key) ? "Guardado. Déjalo vacío para conservarlo." : undefined}>
              {f.textarea ? (
                <textarea rows={4} className="mono" value={secrets[f.key] ?? ""} onChange={(e) => setSecrets({ ...secrets, [f.key]: e.target.value })} />
              ) : (
                <input type="password" autoComplete="off" value={secrets[f.key] ?? ""} onChange={(e) => setSecrets({ ...secrets, [f.key]: e.target.value })} />
              )}
            </Field>
          ) : (
            <Field key={f.key} label={f.label}>
              <input value={config[f.key] ?? ""} onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })} />
            </Field>
          ),
        )}
        <Field label="Conjuntos de datos">
          <div className="grid2">
            {catalog.datasets.map((d) => (
              <label key={d.key} className="inline small" title={`Columnas: ${d.columns.join(", ")}${d.sensitive_columns.length ? `\nSensibles: ${d.sensitive_columns.join(", ")}` : ""}`}>
                <input
                  type="checkbox"
                  checked={datasets.includes(d.key)}
                  onChange={(e) => setDatasets(e.target.checked ? [...datasets, d.key] : datasets.filter((x) => x !== d.key))}
                />
                {DATASET_LABEL[d.key] ?? d.key}
              </label>
            ))}
          </div>
        </Field>
        <div className="grid2">
          <Field label="Formato" hint={destination === "bigquery" ? "BigQuery siempre recibe JSONL." : undefined}>
            <select value={format} disabled={destination === "bigquery"} onChange={(e) => setFormat(e.target.value)}>
              <option value="parquet" disabled={!catalog.parquet}>
                Parquet{!catalog.parquet ? " (no disponible)" : ""}
              </option>
              <option value="csv">CSV</option>
              <option value="jsonl">JSONL</option>
            </select>
          </Field>
          <Field label="Frecuencia">
            <select value={schedule} onChange={(e) => setSchedule(e.target.value)}>
              {catalog.schedules.map((s) => (
                <option key={s} value={s}>
                  {SCHEDULE_LABEL[s] ?? s}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Toggle checked={incremental} onChange={setIncremental} label="Incremental (solo filas nuevas o cambiadas desde la última corrida)" />
        <Toggle
          checked={sensitive}
          onChange={setSensitive}
          label="Incluir datos sensibles (teléfono, nombre, correo, texto de mensajes, identificadores de clic)"
        />
        <Toggle checked={active} onChange={setActive} label="Activa" />
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
