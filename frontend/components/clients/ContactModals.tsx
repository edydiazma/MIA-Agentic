"use client";

import { useState } from "react";
import { api, send, type Contact } from "@/lib/api";
import { ErrorBox, Field, Modal, useAction } from "@/components/ui";

export function NewContactModal({ onClose, onCreated }: { onClose: () => void; onCreated: (c: Contact) => void }) {
  const [waId, setWaId] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [tags, setTags] = useState("");
  const [run, busy, error] = useAction();

  async function submit(e?: React.FormEvent) {
    e?.preventDefault();
    const c = await run(() =>
      send<Contact>("/api/contacts", "POST", {
        wa_id: waId,
        name: name || null,
        email: email || null,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      }),
    );
    if (c) onCreated(c);
  }

  return (
    <Modal
      title="Nuevo cliente"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !waId.trim()} onClick={() => submit()}>
            Crear
          </button>
        </>
      }
    >
      <form className="form" onSubmit={submit}>
        <Field label="Teléfono de WhatsApp" hint="Formato internacional, p. ej. 573001234567">
          <input value={waId} onChange={(e) => setWaId(e.target.value)} required autoFocus />
        </Field>
        <Field label="Nombre">
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Email">
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Etiquetas" hint="Separadas por coma">
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="vip, chevrolet" />
        </Field>
        <ErrorBox error={error} />
      </form>
    </Modal>
  );
}

export function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [tags, setTags] = useState("");
  const [result, setResult] = useState<{ created: number; updated: number; invalid: number } | null>(null);
  const [run, busy, error] = useAction();

  async function submit() {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("tags", tags);
    const r = await run(() =>
      api<{ created: number; updated: number; invalid: number }>("/api/contacts/import", { method: "POST", body: form }),
    );
    if (r) {
      setResult(r);
      onDone();
    }
  }

  return (
    <Modal
      title="Importar clientes desde CSV"
      onClose={onClose}
      footer={
        result ? (
          <button className="primary" onClick={onClose}>
            Listo
          </button>
        ) : (
          <>
            <button onClick={onClose}>Cancelar</button>
            <button className="primary" disabled={busy || !file} onClick={submit}>
              {busy ? "Importando…" : "Importar"}
            </button>
          </>
        )
      }
    >
      <p className="muted small" style={{ margin: 0 }}>
        El archivo debe tener una columna <strong>telefono</strong> (obligatoria) y opcionalmente <strong>nombre</strong> y{" "}
        <strong>email</strong>. Separador coma, punto y coma o tabulación. Los números deben incluir el código de país.
      </p>
      <Field label="Archivo CSV">
        <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </Field>
      <Field label="Etiquetas para todos los importados" hint="Útil para armar la audiencia de una campaña">
        <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="campaña-octubre" />
      </Field>
      <ErrorBox error={error} />
      {result && (
        <div className="stats" style={{ marginBottom: 0 }}>
          <div className="stat ok">
            <span className="stat-label">Creados</span>
            <span className="stat-value">{result.created}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Actualizados</span>
            <span className="stat-value">{result.updated}</span>
          </div>
          <div className={`stat ${result.invalid ? "warn" : ""}`}>
            <span className="stat-label">Inválidos</span>
            <span className="stat-value">{result.invalid}</span>
          </div>
        </div>
      )}
    </Modal>
  );
}
