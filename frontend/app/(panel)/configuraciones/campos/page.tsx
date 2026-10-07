"use client";

import { useState } from "react";
import { send, type ContactField, type FieldType } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

const TYPE_LABEL: Record<FieldType, string> = {
  text: "Texto corto",
  long_text: "Texto largo",
  number: "Número",
  date: "Fecha",
  select: "Lista de opciones",
  boolean: "Sí / No",
  email: "Email",
  phone: "Teléfono",
};

type Draft = Omit<ContactField, "id" | "options"> & { id?: number; optionsText: string };

const empty: Draft = {
  key: "", label: "", type: "text", optionsText: "", description: "", ai_extract: true, agent_editable: true, position: 100,
};

function slug(label: string) {
  const s = label.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 50);
  return /^[a-z]/.test(s) ? s : s ? `campo_${s}`.slice(0, 50) : "";
}

function FieldModal({ initial, onClose, onSaved }: { initial: Draft; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState(initial);
  const [keyTouched, setKeyTouched] = useState(!!initial.id);
  const [run, busy, error] = useAction();
  const editing = !!initial.id;
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }));

  async function save() {
    const body = {
      key: d.key, label: d.label, type: d.type, description: d.description || null, ai_extract: d.ai_extract,
      agent_editable: d.agent_editable, position: d.position,
      options: d.type === "select" ? d.optionsText.split("\n").map((o) => o.trim()).filter(Boolean) : null,
    };
    const r = await run(() => editing ? send(`/api/contact-fields/${d.id}`, "PUT", body) : send("/api/contact-fields", "POST", body));
    if (r) onSaved();
  }

  return (
    <Modal title={editing ? `Editar «${initial.label}»` : "Nuevo campo"} onClose={onClose}
           footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy || !d.label || !d.key} onClick={save}>{busy ? "Guardando…" : "Guardar"}</button></>}>
      <Field label="Nombre visible">
        <input autoFocus value={d.label} onChange={(e) => {
          set("label", e.target.value);
          if (!keyTouched) set("key", slug(e.target.value));
        }} />
      </Field>
      <Field label="Clave" hint={editing ? "La clave no se puede cambiar: los valores guardados dependen de ella." : "Minúsculas, números y _. También sirve como columna del CSV."}>
        <input value={d.key} disabled={editing} onChange={(e) => { setKeyTouched(true); set("key", e.target.value); }} />
      </Field>
      <Field label="Tipo">
        <select value={d.type} onChange={(e) => set("type", e.target.value as FieldType)}>
          {(Object.keys(TYPE_LABEL) as FieldType[]).map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
        </select>
      </Field>
      {d.type === "select" && (
        <Field label="Opciones" hint="Una por línea.">
          <textarea rows={4} value={d.optionsText} onChange={(e) => set("optionsText", e.target.value)} />
        </Field>
      )}
      <Field label="Descripción" hint="También guía a la IA: explica qué dato es y cómo reconocerlo en la conversación.">
        <textarea rows={2} value={d.description ?? ""} onChange={(e) => set("description", e.target.value)} />
      </Field>
      <Toggle checked={d.ai_extract} onChange={(v) => set("ai_extract", v)} label="La IA puede llenarlo a partir de la conversación" />
      <Toggle checked={d.agent_editable} onChange={(v) => set("agent_editable", v)} label="Los asesores pueden editarlo (si no, solo administradores)" />
      <Field label="Orden">
        <input type="number" value={d.position} onChange={(e) => set("position", Number(e.target.value))} />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function CamposPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<ContactField[]>("/api/contact-fields");
  const [editing, setEditing] = useState<Draft | null>(null);
  const [run, , delError] = useAction();

  async function remove(f: ContactField) {
    if (!confirm(`¿Eliminar el campo «${f.label}»? Los valores ya guardados se conservan en el historial de cada cliente, pero dejarán de mostrarse.`)) return;
    await run(async () => {
      await send(`/api/contact-fields/${f.id}`, "DELETE");
      reload();
    });
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Campos personalizados de la ficha del cliente."
                  actions={isAdmin && <button className="primary" onClick={() => setEditing({ ...empty })}>Nuevo campo</button>} />
      <ConfigTabs />
      <AdminNotice />
      <Card>
        <p className="small muted" style={{ marginTop: 0 }}>
          Los asesores llenan estos campos desde la conversación o la ficha del cliente, y la IA puede extraerlos de lo que dice el cliente
          (sin sobrescribir lo que editó una persona). Al importar clientes por CSV, las columnas se asocian por clave o por nombre visible.
        </p>
        <ErrorBox error={error || delError} />
        {loading && !data ? <Loading /> : !data?.length ? (
          <Empty>Aún no hay campos personalizados.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th className="num">Orden</th><th>Nombre</th><th>Clave</th><th>Tipo</th><th>Opciones</th><th>IA</th><th>Asesores</th><th /></tr>
              </thead>
              <tbody>
                {data.map((f) => (
                  <tr key={f.id}>
                    <td className="num">{f.position}</td>
                    <td>
                      <div className="strong">{f.label}</div>
                      {f.description && <div className="small muted">{f.description}</div>}
                    </td>
                    <td><code>{f.key}</code></td>
                    <td>{TYPE_LABEL[f.type]}</td>
                    <td className="small">{f.options?.join(", ") || "—"}</td>
                    <td>{f.ai_extract ? <Badge tone="info">Extrae</Badge> : <Badge>No</Badge>}</td>
                    <td>{f.agent_editable ? <Badge tone="ok">Editable</Badge> : <Badge tone="warn">Solo admin</Badge>}</td>
                    <td className="right nowrap">
                      {isAdmin && (
                        <>
                          <button onClick={() => setEditing({ ...f, optionsText: (f.options ?? []).join("\n") })}>Editar</button>{" "}
                          <button className="danger" onClick={() => remove(f)}>Eliminar</button>
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
      {editing && <FieldModal initial={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }} />}
    </>
  );
}
