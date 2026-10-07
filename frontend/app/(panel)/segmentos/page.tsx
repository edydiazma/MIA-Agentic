"use client";

import { useState } from "react";
import { downloadUrl, fmtDateTime, fmtNum, send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import RuleBuilder, { toGroup } from "@/components/journeys/RuleBuilder";
import type { Segment, SegmentField, SegmentPreview } from "@/lib/journey-types";

export default function SegmentosPage() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const { data, error, loading, reload } = useApi<Segment[]>("/api/segments");
  const [editing, setEditing] = useState<Segment | "new" | null>(null);
  const [run, , actionError] = useAction();
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh(s: Segment) {
    const r = await run(() => send<{ entered: number; left: number; count: number; enrolled: number }>(
      `/api/segments/${s.id}/refresh`, "POST"));
    if (r) {
      setNotice(`«${s.name}»: ${r.count} clientes (${r.entered} entraron, ${r.left} salieron, ${r.enrolled} inscritos en journeys).`);
      reload();
    }
  }

  async function remove(s: Segment) {
    if (!window.confirm(`¿Eliminar el segmento «${s.name}»?`)) return;
    if (await run(() => send(`/api/segments/${s.id}`, "DELETE"))) reload();
  }

  return (
    <>
      <PageHeader
        title="Segmentos"
        subtitle="Audiencias por reglas sobre el cliente 360: fuente, vehículos, consentimientos, negocios, productos y pedidos"
        actions={isAdmin && <button className="primary" onClick={() => setEditing("new")}>Nuevo segmento</button>}
      />
      <Card>
        <ErrorBox error={error || actionError} />
        {notice && <div className="notice small">{notice}</div>}
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>Aún no hay segmentos. Crea uno para usarlo en campañas y journeys.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Segmento</th>
                  <th>Tipo</th>
                  <th className="num">Clientes</th>
                  <th>Calculado</th>
                  <th>Recalcula</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <strong>{s.name}</strong>
                      {s.description && <div className="muted small">{s.description}</div>}
                    </td>
                    <td>{s.kind === "dynamic" ? <Badge tone="info">Dinámico</Badge> : <Badge>Estático</Badge>}</td>
                    <td className="num">{fmtNum(s.member_count)}</td>
                    <td>{fmtDateTime(s.last_computed_at)}</td>
                    <td>{s.kind === "dynamic" ? `cada ${s.refresh_minutes} min` : "—"}</td>
                    <td className="actions">
                      <a className="button" href={downloadUrl(`/api/segments/${s.id}/export.csv`)}>CSV</a>
                      {isAdmin && (
                        <>
                          <button onClick={() => refresh(s)}>Recalcular</button>
                          <button onClick={() => setEditing(s)}>Editar</button>
                          <button className="danger" onClick={() => remove(s)}>Eliminar</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && (
        <SegmentModal segment={editing === "new" ? null : editing} onClose={() => setEditing(null)}
          onDone={() => { setEditing(null); reload(); }} />
      )}
    </>
  );
}

function SegmentModal({ segment, onClose, onDone }: { segment: Segment | null; onClose: () => void; onDone: () => void }) {
  const fields = useApi<{ fields: SegmentField[] }>("/api/segments/fields");
  const [name, setName] = useState(segment?.name ?? "");
  const [description, setDescription] = useState(segment?.description ?? "");
  const [kind, setKind] = useState<"dynamic" | "static">(segment?.kind ?? "dynamic");
  const [refreshMinutes, setRefreshMinutes] = useState(segment?.refresh_minutes ?? 60);
  const [rule, setRule] = useState(toGroup(segment?.definition));
  const [ids, setIds] = useState("");
  const [preview, setPreview] = useState<SegmentPreview | null>(null);
  const [run, busy, error] = useAction();

  async function doPreview() {
    const r = await run(() => send<SegmentPreview>("/api/segments/preview", "POST", { definition: rule }));
    if (r) setPreview(r);
  }

  async function save() {
    const body = {
      name: name.trim(),
      description: description.trim() || null,
      kind,
      refresh_minutes: refreshMinutes,
      definition: kind === "dynamic" ? rule : {},
      contact_ids: ids.split(/[\s,;]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0),
    };
    const ok = await run(() =>
      segment ? send(`/api/segments/${segment.id}`, "PUT", body) : send("/api/segments", "POST", body));
    if (ok) onDone();
  }

  return (
    <Modal
      title={segment ? `Editar «${segment.name}»` : "Nuevo segmento"}
      onClose={onClose}
      wide
      footer={
        <>
          {kind === "dynamic" && <button onClick={doPreview} disabled={busy}>Vista previa</button>}
          <button className="primary" onClick={save} disabled={busy || !name.trim() || (kind === "static" && !ids.trim())}>Guardar</button>
        </>
      }
    >
      <div className="stack">
        <Field label="Nombre"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Descripción"><input value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        {!segment && (
          <Field label="Tipo">
            <div className="inline">
              <label className="inline"><input type="radio" checked={kind === "dynamic"} onChange={() => setKind("dynamic")} /> Dinámico (reglas)</label>
              <label className="inline"><input type="radio" checked={kind === "static"} onChange={() => setKind("static")} /> Estático (lista fija)</label>
            </div>
          </Field>
        )}
        {kind === "dynamic" ? (
          <>
            <Field label="Recalcular cada (minutos)" hint="Quienes entran al segmento pueden iniciar un journey">
              <input type="number" min={5} max={10080} value={refreshMinutes} onChange={(e) => setRefreshMinutes(Number(e.target.value))} />
            </Field>
            {fields.data ? (
              <RuleBuilder value={rule} fields={fields.data.fields} onChange={(g) => { setRule(g); setPreview(null); }} />
            ) : (
              <Loading />
            )}
            {preview && (
              <div className="card">
                <strong>{fmtNum(preview.count)} clientes</strong>
                <ul className="small">
                  {preview.sample.map((c) => (
                    <li key={c.id}>{c.name ?? "Sin nombre"} · {c.wa_id ?? c.email ?? `#${c.id}`} · {c.stage}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <Field label="IDs de clientes" hint={segment ? "Reemplaza la lista actual" : "Separados por coma o salto de línea"}>
            <textarea rows={5} value={ids} onChange={(e) => setIds(e.target.value)} />
          </Field>
        )}
        <ErrorBox error={error || fields.error} />
      </div>
    </Modal>
  );
}
