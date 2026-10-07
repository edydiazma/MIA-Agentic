"use client";

import { useEffect, useState } from "react";
import { api, contactLabel, qs, type Contact, phoneLabel } from "@/lib/api";

/** Buscador de clientes por nombre, teléfono o email. */
export default function ContactPicker({
  value,
  onChange,
}: {
  value: Contact | null;
  onChange: (c: Contact | null) => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Contact[]>([]);

  useEffect(() => {
    if (value || q.trim().length < 2) {
      setResults([]);
      return;
    }
    const t = setTimeout(() => {
      api<{ items: Contact[] }>(`/api/contacts${qs({ q: q.trim(), limit: 8 })}`)
        .then((r) => setResults(r.items))
        .catch(() => setResults([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q, value]);

  if (value)
    return (
      <div className="inline">
        <strong>{contactLabel(value)}</strong>
        <span className="muted">{phoneLabel(value.wa_id)}</span>
        <button type="button" className="link" onClick={() => onChange(null)}>
          Cambiar
        </button>
      </div>
    );

  return (
    <div className="stack" style={{ gap: 6 }}>
      <input placeholder="Buscar por nombre, teléfono o email" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
      {results.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <tbody>
              {results.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => onChange(c)}>
                  <td>{contactLabel(c)}</td>
                  <td className="muted">{phoneLabel(c.wa_id)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {q.trim().length >= 2 && results.length === 0 && <span className="muted small">Sin resultados</span>}
    </div>
  );
}
