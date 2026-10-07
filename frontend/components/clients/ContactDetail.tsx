"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  STAGE_LABEL,
  STATUS_LABEL,
  daysAgo,
  fmtDateTime,
  isoDay,
  qs,
  send,
  type Appointment,
  type Contact,
  type Conversation,
  type FollowUp,
  type Stage,
} from "@/lib/api";
import { Badge, ErrorBox, Field, Loading, Modal, useAction, useApi } from "@/components/ui";
import CustomFieldsEditor from "@/components/fields/CustomFieldsEditor";
import FieldHistory from "@/components/fields/FieldHistory";

type Detail = Contact & { conversations: Conversation[] };

const APPT_LABEL: Record<Appointment["status"], string> = {
  scheduled: "Agendada",
  done: "Realizada",
  cancelled: "Cancelada",
  no_show: "No asistió",
};

function plusDays(n: number) {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return isoDay(d);
}

export default function ContactDetail({
  contactId,
  onClose,
  onChanged,
}: {
  contactId: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { data, error, loading, setData } = useApi<Detail>(`/api/contacts/${contactId}`);
  const followups = useApi<FollowUp[]>(`/api/followups${qs({ scope: "all", status: "all", contact_id: contactId })}`);
  const appts = useApi<Appointment[]>(
    `/api/appointments${qs({ contact_id: contactId, start: daysAgo(90), end: plusDays(90) })}`,
  );
  const [form, setForm] = useState({ name: "", email: "", stage: "lead" as Stage, tags: "", notes: "" });
  const [saved, setSaved] = useState(false);
  const [historyKey, setHistoryKey] = useState(0);
  const [run, busy, actionError] = useAction();

  useEffect(() => {
    if (data)
      setForm({
        name: data.name ?? "",
        email: data.email ?? "",
        stage: data.stage,
        tags: data.tags.join(", "),
        notes: data.notes ?? "",
      });
  }, [data]);

  async function save() {
    setSaved(false);
    const c = await run(() =>
      send<Contact>(`/api/contacts/${contactId}`, "PUT", {
        name: form.name || null,
        email: form.email || null,
        stage: form.stage,
        notes: form.notes || null,
        tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
      }),
    );
    if (c && data) {
      setData({ ...data, ...c });
      setSaved(true);
      setHistoryKey((n) => n + 1);
      onChanged();
    }
  }

  async function block() {
    const reason = window.prompt("Motivo del bloqueo (opcional). Sus mensajes se ignorarán y se cerrarán sus conversaciones.");
    if (reason === null) return;
    const ok = await run(() => send(`/api/contacts/${contactId}/block`, "POST", { reason: reason || null }));
    if (ok) {
      onChanged();
      onClose();
    }
  }

  return (
    <Modal
      wide
      title={data ? data.name || `+${data.wa_id}` : "Cliente"}
      onClose={onClose}
      footer={
        <>
          <button className="danger" onClick={block} disabled={busy || !data}>
            Bloquear
          </button>
          <span style={{ flex: 1 }} />
          {saved && <span className="muted small">Guardado</span>}
          <button onClick={onClose}>Cerrar</button>
          <button className="primary" onClick={save} disabled={busy || !data}>
            Guardar
          </button>
        </>
      }
    >
      {loading && !data && <Loading />}
      <ErrorBox error={error || actionError} />
      {data && (
        <>
          <div className="inline">
            <span className="muted">+{data.wa_id}</span>
            {data.marketing_opt_out && <Badge tone="warn">No acepta marketing</Badge>}
            <span className="muted small">Cliente desde {fmtDateTime(data.created_at)}</span>
          </div>
          <div className="grid2">
            <Field label="Nombre">
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
            <Field label="Email">
              <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </Field>
            <Field label="Etapa">
              <select value={form.stage} onChange={(e) => setForm({ ...form, stage: e.target.value as Stage })}>
                {(Object.keys(STAGE_LABEL) as Stage[]).map((s) => (
                  <option key={s} value={s}>
                    {STAGE_LABEL[s]}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Etiquetas" hint="Separadas por coma">
              <input value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
            </Field>
          </div>
          <Field label="Notas">
            <textarea rows={3} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>

          <div>
            <h2 style={{ marginBottom: 6 }}>Campos</h2>
            <CustomFieldsEditor
              contact={data}
              onSaved={(c) => {
                setData({ ...data, ...c });
                setHistoryKey((n) => n + 1);
                onChanged();
              }}
            />
          </div>

          <div>
            <h2 style={{ marginBottom: 6 }}>Conversaciones</h2>
            {data.conversations.length === 0 ? (
              <p className="muted small">Sin conversaciones.</p>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Estado</th>
                      <th>Tipificación</th>
                      <th>Asesor</th>
                      <th>Último mensaje</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {data.conversations.map((c) => (
                      <tr key={c.id}>
                        <td>
                          <span className={`status ${c.status}`}>{STATUS_LABEL[c.status]}</span>
                        </td>
                        <td>{c.typification ?? "—"}</td>
                        <td>{c.assigned_agent?.name ?? "—"}</td>
                        <td>{fmtDateTime(c.last_message_at)}</td>
                        <td className="right">
                          <Link href={`/conversaciones?id=${c.id}`}>Abrir</Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="grid2">
            <div>
              <h2 style={{ marginBottom: 6 }}>Seguimientos</h2>
              {!followups.data?.length ? (
                <p className="muted small">Sin seguimientos.</p>
              ) : (
                <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {followups.data.map((f) => (
                    <li key={f.id}>
                      {fmtDateTime(f.due_at)} · {f.note} <span className="muted">({f.agent_name})</span>{" "}
                      {f.done ? <Badge tone="ok">Hecho</Badge> : f.overdue ? <Badge tone="bad">Vencido</Badge> : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <h2 style={{ marginBottom: 6 }}>Citas</h2>
              {!appts.data?.length ? (
                <p className="muted small">Sin citas en los últimos/próximos 90 días.</p>
              ) : (
                <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {appts.data.map((a) => (
                    <li key={a.id}>
                      {fmtDateTime(a.starts_at)} · {a.title} <Badge>{APPT_LABEL[a.status]}</Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div>
            <h2 style={{ marginBottom: 6 }}>Historial de cambios</h2>
            <FieldHistory contactId={contactId} reloadKey={historyKey} />
          </div>
        </>
      )}
    </Modal>
  );
}
