"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { qs, send, type Contact, type ContactField, type FieldChange } from "@/lib/api";
import { useMe } from "@/components/Shell";
import { Badge, ErrorBox, Loading, useAction, useApi } from "@/components/ui";

type Value = string | number | boolean | undefined | null;

function toInput(v: Value): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

/** Editor de los campos personalizados de la ficha (los define un admin en Configuraciones → Campos de cliente). */
export default function CustomFieldsEditor({
  contact,
  conversationId,
  onSaved,
  compact,
}: {
  contact: Contact;
  conversationId?: number;
  onSaved?: (c: Contact) => void;
  compact?: boolean;
}) {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const fields = useApi<ContactField[]>("/api/contact-fields");
  const [historyKey, setHistoryKey] = useState(0);
  const history = useApi<FieldChange[]>(`/api/contacts/${contact.id}/history?_=${historyKey}`);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [run, busy, error] = useAction();

  const original = useMemo(() => {
    const out: Record<string, string> = {};
    for (const f of fields.data ?? []) out[f.key] = toInput(contact.custom_fields?.[f.key]);
    return out;
  }, [fields.data, contact.custom_fields]);

  useEffect(() => {
    setValues(original);
    setSaved(false);
  }, [original, contact.id]);

  // Último origen de cada campo: si lo llenó la IA, se marca con "IA".
  const lastSource = useMemo(() => {
    const out: Record<string, FieldChange["source"]> = {};
    for (const h of history.data ?? []) if (!(h.field_key in out)) out[h.field_key] = h.source; // viene ordenado desc
    return out;
  }, [history.data]);

  const changed = Object.keys(values).filter((k) => values[k] !== original[k]);

  async function save() {
    const payload: Record<string, string | boolean | null> = {};
    for (const k of changed) {
      const f = fields.data?.find((x) => x.key === k);
      const v = values[k];
      payload[k] = v === "" ? null : f?.type === "boolean" ? v === "true" : v;
    }
    const c = await run(() =>
      send<Contact>(`/api/contacts/${contact.id}${qs({ conversation_id: conversationId })}`, "PUT", { custom_fields: payload }),
    );
    if (c) {
      setSaved(true);
      setHistoryKey((n) => n + 1);
      onSaved?.(c);
    }
  }

  if (fields.loading && !fields.data) return <Loading />;
  if (fields.error) return <ErrorBox error={fields.error} />;
  if (!fields.data?.length)
    return (
      <p className="muted small" style={{ margin: 0 }}>
        No hay campos personalizados. Un administrador los define en{" "}
        {isAdmin ? <Link href="/configuraciones/campos">Configuraciones → Campos de cliente</Link> : "Configuraciones → Campos de cliente"}.
      </p>
    );

  return (
    <div className="stack" style={{ gap: compact ? 8 : 12 }}>
      {fields.data.map((f) => {
        const locked = !f.agent_editable && !isAdmin;
        const v = values[f.key] ?? "";
        const set = (nv: string) => setValues((prev) => ({ ...prev, [f.key]: nv }));
        const common = { disabled: locked || busy, "aria-label": f.label };
        let input: React.ReactNode;
        if (f.type === "long_text") input = <textarea rows={3} value={v} onChange={(e) => set(e.target.value)} {...common} />;
        else if (f.type === "select")
          input = (
            <select value={v} onChange={(e) => set(e.target.value)} {...common}>
              <option value="">—</option>
              {(f.options ?? []).map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          );
        else if (f.type === "boolean")
          input = (
            <select value={v} onChange={(e) => set(e.target.value)} {...common}>
              <option value="">—</option>
              <option value="true">Sí</option>
              <option value="false">No</option>
            </select>
          );
        else
          input = (
            <input
              type={{ number: "text", date: "date", email: "email", phone: "tel" }[f.type as string] ?? "text"}
              inputMode={f.type === "number" ? "decimal" : undefined}
              value={v}
              onChange={(e) => set(e.target.value)}
              {...common}
            />
          );
        return (
          <label key={f.key} className="field">
            <span className="inline" style={{ gap: 6 }}>
              {f.label}
              {locked && <span title="Solo un administrador puede editarlo">🔒</span>}
              {lastSource[f.key] === "ai" && v === original[f.key] && v !== "" && (
                <Badge tone="info">IA</Badge>
              )}
              {v !== original[f.key] && <span className="muted small">• sin guardar</span>}
            </span>
            {input}
            {f.description && !compact && <small className="muted">{f.description}</small>}
          </label>
        );
      })}
      <ErrorBox error={error} />
      <div className="row">
        <button className="primary" disabled={busy || changed.length === 0} onClick={save}>
          Guardar campos{changed.length ? ` (${changed.length})` : ""}
        </button>
        {saved && changed.length === 0 && <span className="muted small">Guardado</span>}
      </div>
    </div>
  );
}
