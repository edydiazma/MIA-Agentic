"use client";

import { useRef, useState } from "react";
import { api, fmtDateTime, fmtNum, send, type KnowledgeDoc } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, Toggle, useAction, useApi } from "@/components/ui";
import { useIsAdmin } from "./common";

const WARN_CHARS = 600_000;

type Draft = { id?: number; title: string; content: string; enabled: boolean };

export default function KnowledgeBase({ botId }: { botId: number }) {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<KnowledgeDoc[]>(`/api/knowledge?bot_id=${botId}`);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const fileRef = useRef<HTMLInputElement>(null);

  const total = (data ?? []).filter((d) => d.enabled).reduce((s, d) => s + d.chars, 0);

  async function save() {
    if (!draft) return;
    const ok = await run(() =>
      draft.id
        ? send(`/api/knowledge/${draft.id}`, "PUT", { title: draft.title, content: draft.content, enabled: draft.enabled })
        : send("/api/knowledge", "POST", { bot_id: botId, title: draft.title, content: draft.content, enabled: draft.enabled }),
    );
    if (ok) {
      setDraft(null);
      reload();
    }
  }

  async function upload(file: File) {
    const form = new FormData();
    form.append("bot_id", String(botId));
    form.append("file", file);
    await run(() => api("/api/knowledge/upload", { method: "POST", body: form }));
    reload();
  }

  async function toggle(d: KnowledgeDoc, enabled: boolean) {
    await run(() => send(`/api/knowledge/${d.id}`, "PUT", { enabled }));
    reload();
  }

  async function remove(d: KnowledgeDoc) {
    if (!confirm(`¿Eliminar «${d.title}» de la base de conocimiento?`)) return;
    await run(() => send(`/api/knowledge/${d.id}`, "DELETE"));
    reload();
  }

  return (
    <Card
      title="Base de conocimiento"
      actions={
        isAdmin && (
          <>
            <button onClick={() => { setActionError(null); setDraft({ title: "", content: "", enabled: true }); }}>Agregar texto</button>
            <button className="primary" disabled={busy} onClick={() => fileRef.current?.click()}>Subir PDF / texto</button>
            <input
              ref={fileRef}
              type="file"
              hidden
              accept=".pdf,.txt,.md,.csv,.json"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) upload(f);
                e.target.value = "";
              }}
            />
          </>
        )
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        El agente responde usando estos documentos (precios, catálogo, políticas, preguntas frecuentes). Activos:{" "}
        <strong>{fmtNum(total)}</strong> caracteres.
      </p>
      {total > WARN_CHARS && (
        <div className="error-box" style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>
          La base de conocimiento es muy grande: cada respuesta será más lenta y costosa. Desactiva documentos que no se usen.
        </div>
      )}
      <ErrorBox error={error || (!draft ? actionError : null)} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Empty>Sin documentos. Sube el catálogo, la lista de precios o las preguntas frecuentes.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Título</th><th>Origen</th><th className="num">Caracteres</th><th>Actualizado</th><th>Activo</th><th /></tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.id}>
                  <td className="strong">{d.title}</td>
                  <td className="small muted">{d.source_filename ? <Badge>{d.source_filename}</Badge> : "Texto"}</td>
                  <td className="num">{fmtNum(d.chars)}</td>
                  <td className="small nowrap">{fmtDateTime(d.updated_at)}</td>
                  <td><Toggle checked={d.enabled} onChange={(v) => isAdmin && toggle(d, v)} /></td>
                  <td className="nowrap">
                    {isAdmin && (
                      <div className="inline">
                        <button onClick={() => { setActionError(null); setDraft({ id: d.id, title: d.title, content: d.content, enabled: d.enabled }); }}>Editar</button>
                        <button className="danger" onClick={() => remove(d)} disabled={busy}>Eliminar</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {draft && (
        <Modal
          wide
          title={draft.id ? "Editar documento" : "Nuevo documento"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.title.trim() || !draft.content.trim()}>Guardar</button>
            </>
          }
        >
          <Field label="Título"><input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></Field>
          <Field label="Contenido">
            <textarea rows={16} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
          </Field>
          <Toggle checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} label="Activo" />
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </Card>
  );
}
