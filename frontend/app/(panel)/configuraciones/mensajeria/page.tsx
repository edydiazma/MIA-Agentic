"use client";

import { useState } from "react";
import { send, type QuickReply, type Template } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

type Draft = { id?: number; shortcut: string; text: string };

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "neutral"> = {
  APPROVED: "ok",
  PENDING: "warn",
  PAUSED: "warn",
  REJECTED: "bad",
  DISABLED: "bad",
};

export default function MensajeriaPage() {
  const isAdmin = useIsAdmin();
  const qr = useApi<QuickReply[]>("/api/quick-replies");
  const tpl = useApi<Template[]>("/api/templates");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  async function save() {
    if (!draft) return;
    const body = { shortcut: draft.shortcut, text: draft.text };
    const ok = await run(() => (draft.id ? send(`/api/quick-replies/${draft.id}`, "PUT", body) : send("/api/quick-replies", "POST", body)));
    if (ok) {
      setDraft(null);
      qr.reload();
    }
  }

  async function remove(q: QuickReply) {
    if (!confirm(`¿Eliminar /${q.shortcut}?`)) return;
    await run(() => send(`/api/quick-replies/${q.id}`, "DELETE"));
    qr.reload();
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Mensajería: respuestas rápidas y plantillas de WhatsApp." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={!draft ? actionError : null} />

      <Card
        title="Respuestas rápidas"
        actions={isAdmin && <button className="primary" onClick={() => { setActionError(null); setDraft({ shortcut: "", text: "" }); }}>Nueva respuesta</button>}
      >
        <p className="small muted" style={{ marginTop: 0 }}>Los asesores las insertan escribiendo <code>/atajo</code> en el chat.</p>
        <ErrorBox error={qr.error} />
        {qr.loading && !qr.data ? (
          <Loading />
        ) : !qr.data?.length ? (
          <Empty>Sin respuestas rápidas.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Atajo</th><th>Texto</th><th /></tr></thead>
              <tbody>
                {qr.data.map((q) => (
                  <tr key={q.id}>
                    <td><code>/{q.shortcut}</code></td>
                    <td style={{ whiteSpace: "pre-wrap" }}>{q.text}</td>
                    <td className="nowrap">
                      {isAdmin && (
                        <div className="inline">
                          <button onClick={() => { setActionError(null); setDraft({ ...q }); }}>Editar</button>
                          <button className="danger" onClick={() => remove(q)} disabled={busy}>Eliminar</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Plantillas de WhatsApp" actions={<button onClick={() => tpl.reload()} disabled={tpl.loading}>Recargar</button>}>
        <p className="small muted" style={{ marginTop: 0 }}>
          Se crean y aprueban en el WhatsApp Manager de Meta. Aquí se listan para usarlas en campañas y fuera de la ventana de 24 h.
        </p>
        <ErrorBox error={tpl.error} />
        {tpl.loading && !tpl.data ? (
          <Loading />
        ) : !tpl.data?.length ? (
          <Empty>No hay plantillas (o falta configurar WA_WABA_ID).</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Nombre</th><th>Idioma</th><th>Categoría</th><th>Estado</th><th>Contenido</th><th>Uso</th></tr></thead>
              <tbody>
                {tpl.data.map((t) => (
                  <tr key={`${t.name}-${t.language}`}>
                    <td className="strong">{t.name}</td>
                    <td>{t.language}</td>
                    <td className="small">{t.category}</td>
                    <td><Badge tone={STATUS_TONE[t.status] ?? "neutral"}>{t.status}</Badge></td>
                    <td className="small" style={{ whiteSpace: "pre-wrap", maxWidth: 420 }}>
                      {t.header && <strong>{t.header}{"\n"}</strong>}
                      {t.body}
                    </td>
                    <td className="small">
                      {t.supported ? <Badge tone="ok">Soportada</Badge> : <span className="muted">{t.unsupported_reason}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {draft && (
        <Modal
          title={draft.id ? "Editar respuesta rápida" : "Nueva respuesta rápida"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.shortcut.trim() || !draft.text.trim()}>Guardar</button>
            </>
          }
        >
          <Field label="Atajo" hint="Sin espacios, p. ej. saludo">
            <input value={draft.shortcut} onChange={(e) => setDraft({ ...draft, shortcut: e.target.value.replace(/\s/g, "") })} />
          </Field>
          <Field label="Texto">
            <textarea rows={5} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          </Field>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
