"use client";

import { useState } from "react";
import Link from "next/link";
import { fmtDateTime, qs, send, type Contact, type FollowUp } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, Tabs, useAction, useApi } from "@/components/ui";
import ContactPicker from "./ContactPicker";

/** Valor para <input type="datetime-local"> en hora local. */
export function toLocalInput(iso: string | Date): string {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function NewFollowUpModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [contact, setContact] = useState<Contact | null>(null);
  const [due, setDue] = useState(() => toLocalInput(new Date(Date.now() + 24 * 3600 * 1000)));
  const [note, setNote] = useState("");
  const [run, busy, error] = useAction();

  async function submit() {
    if (!contact) return;
    const ok = await run(() =>
      send("/api/followups", "POST", { contact_id: contact.id, due_at: new Date(due).toISOString(), note }),
    );
    if (ok) {
      onDone();
      onClose();
    }
  }

  return (
    <Modal
      title="Nuevo seguimiento"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !contact || !note.trim() || !due} onClick={submit}>
            Crear
          </button>
        </>
      }
    >
      <Field label="Cliente">
        <ContactPicker value={contact} onChange={setContact} />
      </Field>
      <Field label="Vence">
        <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
      </Field>
      <Field label="Nota">
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Llamar para confirmar la cotización" />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function FollowUps() {
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [status, setStatus] = useState<"pending" | "done" | "all">("pending");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<{ id: number; due: string } | null>(null);
  const { data, error, loading, reload } = useApi<FollowUp[]>(`/api/followups${qs({ scope, status })}`);
  const [run, busy, actionError] = useAction();

  async function update(f: FollowUp, body: Record<string, unknown>) {
    if (await run(() => send(`/api/followups/${f.id}`, "PUT", body))) reload();
  }

  async function remove(f: FollowUp) {
    if (!window.confirm("¿Eliminar este seguimiento?")) return;
    if (await run(() => send(`/api/followups/${f.id}`, "DELETE"))) reload();
  }

  return (
    <Card
      title="Seguimientos"
      actions={
        <>
          <Tabs value={scope} onChange={setScope} tabs={[["mine", "Mis seguimientos"], ["all", "Todos"]]} />
          <select style={{ width: "auto" }} value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
            <option value="pending">Pendientes</option>
            <option value="done">Hechos</option>
            <option value="all">Todos</option>
          </select>
          <button className="primary" onClick={() => setCreating(true)}>
            Nuevo seguimiento
          </button>
        </>
      }
    >
      <ErrorBox error={error || actionError} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Empty>No hay seguimientos. También puedes crearlos desde una conversación.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Vence</th>
                <th>Cliente</th>
                <th>Nota</th>
                <th>Asesor</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((f) => (
                <tr key={f.id}>
                  <td className="nowrap">
                    {editing?.id === f.id ? (
                      <div className="inline">
                        <input
                          type="datetime-local"
                          style={{ width: "auto" }}
                          value={editing.due}
                          onChange={(e) => setEditing({ id: f.id, due: e.target.value })}
                        />
                        <button
                          className="primary"
                          disabled={busy}
                          onClick={async () => {
                            await update(f, { due_at: new Date(editing.due).toISOString() });
                            setEditing(null);
                          }}
                        >
                          OK
                        </button>
                        <button onClick={() => setEditing(null)}>✕</button>
                      </div>
                    ) : (
                      <>
                        {fmtDateTime(f.due_at)}{" "}
                        {f.done ? <Badge tone="ok">Hecho</Badge> : f.overdue && <Badge tone="bad">Vencido</Badge>}
                      </>
                    )}
                  </td>
                  <td>
                    {f.conversation_id ? (
                      <Link href={`/conversaciones?id=${f.conversation_id}`}>{f.contact_name || `+${f.wa_id}`}</Link>
                    ) : (
                      f.contact_name || `+${f.wa_id}`
                    )}
                  </td>
                  <td style={{ whiteSpace: "pre-wrap" }}>{f.note}</td>
                  <td>{f.agent_name}</td>
                  <td className="right nowrap">
                    {!f.done ? (
                      <>
                        <button disabled={busy} onClick={() => update(f, { done: true })}>
                          ✓ Hecho
                        </button>{" "}
                        <button disabled={busy} onClick={() => setEditing({ id: f.id, due: toLocalInput(f.due_at) })}>
                          Reprogramar
                        </button>{" "}
                      </>
                    ) : (
                      <>
                        <button disabled={busy} onClick={() => update(f, { done: false })}>
                          Reabrir
                        </button>{" "}
                      </>
                    )}
                    <button className="danger" disabled={busy} onClick={() => remove(f)}>
                      Eliminar
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating && <NewFollowUpModal onClose={() => setCreating(false)} onDone={reload} />}
    </Card>
  );
}
