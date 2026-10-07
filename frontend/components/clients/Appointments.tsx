"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, fmtTime, isoDay, qs, send, type Appointment, type Contact } from "@/lib/api";
import { Card, DateRange, Empty, ErrorBox, Field, Loading, Modal, useAction, useApi } from "@/components/ui";
import ContactPicker from "./ContactPicker";

const STATUS: Record<Appointment["status"], string> = {
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

function NewAppointmentModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [contact, setContact] = useState<Contact | null>(null);
  const [day, setDay] = useState(plusDays(1));
  const [slots, setSlots] = useState<string[] | null>(null);
  const [slot, setSlot] = useState("");
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [run, busy, error] = useAction();

  useEffect(() => {
    setSlot("");
    setSlots(null);
    if (!day) return;
    api<string[]>(`/api/appointments/availability${qs({ day })}`)
      .then(setSlots)
      .catch(() => setSlots([]));
  }, [day]);

  async function submit() {
    if (!contact || !slot) return;
    const ok = await run(() =>
      send("/api/appointments", "POST", {
        contact_id: contact.id,
        starts_at: new Date(slot).toISOString(),
        title: title || null,
        notes: notes || null,
      }),
    );
    if (ok) {
      onDone();
      onClose();
    }
  }

  return (
    <Modal
      title="Nueva cita"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !contact || !slot} onClick={submit}>
            Agendar
          </button>
        </>
      }
    >
      <Field label="Cliente">
        <ContactPicker value={contact} onChange={setContact} />
      </Field>
      <Field label="Fecha">
        <input type="date" value={day} min={isoDay()} onChange={(e) => setDay(e.target.value)} />
      </Field>
      <Field label="Horario" hint="Según la configuración de Citas (días, horario y capacidad)">
        {slots === null ? (
          <span className="muted small">Consultando disponibilidad…</span>
        ) : slots.length === 0 ? (
          <span className="muted small">No hay horarios libres ese día.</span>
        ) : (
          <div className="chips">
            {slots.map((s) => (
              <button key={s} type="button" className={slot === s ? "chip active" : "chip"} onClick={() => setSlot(s)}>
                {fmtTime(s)}
              </button>
            ))}
          </div>
        )}
      </Field>
      <Field label="Título" hint="Vacío = el título configurado en Citas">
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field label="Notas">
        <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function Appointments() {
  const [start, setStart] = useState(isoDay());
  const [end, setEnd] = useState(plusDays(14));
  const [creating, setCreating] = useState(false);
  const { data, error, loading, reload } = useApi<Appointment[]>(`/api/appointments${qs({ start, end })}`);
  const [run, busy, actionError] = useAction();

  const byDay = useMemo(() => {
    const groups = new Map<string, Appointment[]>();
    for (const a of data ?? []) {
      const key = isoDay(new Date(a.starts_at));
      groups.set(key, [...(groups.get(key) ?? []), a]);
    }
    return [...groups.entries()];
  }, [data]);

  async function setStatus(a: Appointment, status: string) {
    if (await run(() => send(`/api/appointments/${a.id}`, "PUT", { status }))) reload();
  }

  return (
    <Card
      title="Citas"
      actions={
        <>
          <DateRange
            start={start}
            end={end}
            onChange={(s, e) => {
              setStart(s);
              setEnd(e);
            }}
          />
          <button className="primary" onClick={() => setCreating(true)}>
            Nueva cita
          </button>
        </>
      }
    >
      <ErrorBox error={error || actionError} />
      {loading && !data ? (
        <Loading />
      ) : !byDay.length ? (
        <Empty>No hay citas en este rango. El bot puede agendarlas si activas Citas en Configuraciones.</Empty>
      ) : (
        <div className="stack">
          {byDay.map(([day, items]) => (
            <div key={day}>
              <h2 style={{ textTransform: "capitalize", marginBottom: 6 }}>
                {new Date(`${day}T12:00:00`).toLocaleDateString("es", { weekday: "long", day: "numeric", month: "long" })}
              </h2>
              <div className="table-wrap">
                <table className="table">
                  <tbody>
                    {items.map((a) => (
                      <tr key={a.id}>
                        <td className="nowrap strong" style={{ width: 70 }}>
                          {fmtTime(a.starts_at)}
                        </td>
                        <td>
                          {a.conversation_id ? (
                            <Link href={`/conversaciones?id=${a.conversation_id}`}>{a.contact_name || `+${a.wa_id}`}</Link>
                          ) : (
                            a.contact_name || `+${a.wa_id}`
                          )}
                          <div className="muted small">+{a.wa_id}</div>
                        </td>
                        <td>
                          {a.title} <span className="muted small">· {a.duration_min} min</span>
                          {a.notes && <div className="muted small">{a.notes}</div>}
                        </td>
                        <td className="nowrap small">{a.created_by === "bot" ? "🤖 Bot" : "Asesor"}</td>
                        <td style={{ width: 150 }}>
                          <select value={a.status} disabled={busy} onChange={(e) => setStatus(a, e.target.value)}>
                            {(Object.keys(STATUS) as Appointment["status"][]).map((s) => (
                              <option key={s} value={s}>
                                {STATUS[s]}
                              </option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
      {creating && <NewAppointmentModal onClose={() => setCreating(false)} onDone={reload} />}
    </Card>
  );
}
