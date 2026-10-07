"use client";

import { useEffect, useState } from "react";
import {
  STAGE_LABEL,
  api,
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
import { Badge, ErrorBox, useAction, useApi } from "@/components/ui";
import CustomFieldsEditor from "@/components/fields/CustomFieldsEditor";
import FieldHistory from "@/components/fields/FieldHistory";
import AIAnalysis from "./AIAnalysis";
import DealsSection from "@/components/crm/DealsSection";
import ConversationOrigin from "@/components/attribution/ConversationOrigin";

function in60Days() {
  const d = new Date();
  d.setDate(d.getDate() + 60);
  return isoDay(d);
}

export default function ContactPanel({
  conversation,
  onContact,
  onConversation,
}: {
  conversation: Conversation;
  onContact: (c: Contact) => void;
  onConversation: (c: Conversation) => void;
}) {
  const [showHistory, setShowHistory] = useState(false);
  const [historyKey, setHistoryKey] = useState(0);
  const contact = conversation.contact;
  const [form, setForm] = useState({ name: "", email: "", stage: "lead" as Stage, tags: "", notes: "" });
  const [saved, setSaved] = useState(false);
  const [run, busy, error] = useAction();

  const followups = useApi<FollowUp[]>(`/api/followups${qs({ scope: "all", status: "pending", contact_id: contact.id })}`);
  const appts = useApi<Appointment[]>(`/api/appointments${qs({ contact_id: contact.id, start: isoDay(), end: in60Days() })}`);

  useEffect(() => {
    setForm({
      name: contact.name ?? "",
      email: contact.email ?? "",
      stage: contact.stage,
      tags: contact.tags.join(", "),
      notes: contact.notes ?? "",
    });
    setSaved(false);
  }, [contact.id, contact.name, contact.email, contact.stage, contact.notes, contact.tags.join(",")]);

  async function save() {
    const c = await run(() =>
      send<Contact>(`/api/contacts/${contact.id}`, "PUT", {
        name: form.name.trim() || null,
        email: form.email.trim() || null,
        stage: form.stage,
        tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
        notes: form.notes.trim() || null,
      }),
    );
    if (c) {
      onContact(c);
      setSaved(true);
      setHistoryKey((n) => n + 1);
    }
  }

  // --- Seguimiento ---
  const [fuDue, setFuDue] = useState("");
  const [fuNote, setFuNote] = useState("");
  async function addFollowup() {
    const ok = await run(() =>
      send<FollowUp>("/api/followups", "POST", {
        contact_id: contact.id,
        conversation_id: conversation.id,
        due_at: new Date(fuDue).toISOString(),
        note: fuNote.trim(),
      }),
    );
    if (ok) {
      setFuDue("");
      setFuNote("");
      followups.reload();
    }
  }

  // --- Citas ---
  const [day, setDay] = useState("");
  const [slots, setSlots] = useState<string[] | null>(null);
  const [slot, setSlot] = useState("");
  async function loadSlots(d: string) {
    setDay(d);
    setSlot("");
    setSlots(null);
    if (!d) return;
    const s = await run(() => api<string[]>(`/api/appointments/availability${qs({ day: d })}`));
    setSlots(s ?? []);
  }
  async function book() {
    const ok = await run(() =>
      send<Appointment>("/api/appointments", "POST", {
        contact_id: contact.id,
        conversation_id: conversation.id,
        starts_at: slot,
      }),
    );
    if (ok) {
      setDay("");
      setSlots(null);
      setSlot("");
      appts.reload();
    }
  }

  return (
    <aside className="side-panel">
      <AIAnalysis conversation={conversation} onConversation={onConversation} />

      <div>
        <h3>Cliente</h3>
        <div className="inline" style={{ marginBottom: 8 }}>
          <span className="muted small">+{contact.wa_id}</span>
          {contact.marketing_opt_out && <Badge tone="warn">Sin marketing</Badge>}
          {contact.blocked && <Badge tone="bad">Bloqueado</Badge>}
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <input placeholder="Nombre" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input placeholder="Email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <select value={form.stage} onChange={(e) => setForm({ ...form, stage: e.target.value as Stage })} aria-label="Etapa">
            {(Object.keys(STAGE_LABEL) as Stage[]).map((s) => (
              <option key={s} value={s}>
                {STAGE_LABEL[s]}
              </option>
            ))}
          </select>
          <input
            placeholder="Etiquetas (separadas por coma)"
            value={form.tags}
            onChange={(e) => setForm({ ...form, tags: e.target.value })}
          />
          <textarea placeholder="Notas" rows={3} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          <div className="row">
            <button className="primary" disabled={busy} onClick={save}>
              Guardar
            </button>
            {saved && <span className="muted small">Guardado</span>}
          </div>
        </div>
      </div>

      <div>
        <h3>Campos del cliente</h3>
        <CustomFieldsEditor
          contact={contact}
          conversationId={conversation.id}
          compact
          onSaved={(c) => {
            onContact(c);
            setHistoryKey((n) => n + 1);
          }}
        />
        <button className="link small" style={{ marginTop: 8 }} onClick={() => setShowHistory((v) => !v)}>
          {showHistory ? "Ocultar historial de cambios" : "Ver historial de cambios"}
        </button>
        {showHistory && (
          <div style={{ marginTop: 8 }}>
            <FieldHistory contactId={contact.id} reloadKey={historyKey + (conversation.ai_classified_at ? 1 : 0)} />
          </div>
        )}
      </div>

      <ConversationOrigin key={conversation.id} conversationId={conversation.id} adHeadline={conversation.ad_headline} />

      <DealsSection key={contact.id} contact={contact} conversationId={conversation.id} />

      <div>
        <h3>Seguimientos</h3>
        {(followups.data ?? []).map((f) => (
          <div key={f.id} className="small" style={{ marginBottom: 6 }}>
            <span style={{ color: f.overdue ? "var(--bad)" : undefined }}>{fmtDateTime(f.due_at)}</span> ·{" "}
            <span className="muted">{f.agent_name}</span>
            <div>{f.note}</div>
          </div>
        ))}
        <div className="stack" style={{ gap: 6 }}>
          <input type="datetime-local" value={fuDue} onChange={(e) => setFuDue(e.target.value)} aria-label="Fecha del seguimiento" />
          <input placeholder="Nota del seguimiento" value={fuNote} onChange={(e) => setFuNote(e.target.value)} />
          <button disabled={busy || !fuDue || !fuNote.trim()} onClick={addFollowup}>
            Nuevo seguimiento
          </button>
        </div>
      </div>

      <div>
        <h3>Citas</h3>
        {(appts.data ?? [])
          .filter((a) => a.status === "scheduled")
          .map((a) => (
            <div key={a.id} className="small" style={{ marginBottom: 6 }}>
              📅 {fmtDateTime(a.starts_at)} · {a.title}
              {a.created_by === "bot" && <span className="muted"> (bot)</span>}
            </div>
          ))}
        <div className="stack" style={{ gap: 6 }}>
          <input type="date" value={day} min={isoDay()} onChange={(e) => loadSlots(e.target.value)} aria-label="Día de la cita" />
          {slots && slots.length === 0 && <span className="muted small">Sin horarios libres ese día.</span>}
          {slots && slots.length > 0 && (
            <select value={slot} onChange={(e) => setSlot(e.target.value)} aria-label="Horario">
              <option value="">Horario…</option>
              {slots.map((s) => (
                <option key={s} value={s}>
                  {new Date(s).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })}
                </option>
              ))}
            </select>
          )}
          <button disabled={busy || !slot} onClick={book}>
            Agendar cita
          </button>
        </div>
      </div>
      <ErrorBox error={error} />
    </aside>
  );
}
