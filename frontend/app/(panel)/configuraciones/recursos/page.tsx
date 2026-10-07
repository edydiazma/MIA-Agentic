"use client";

import { useRef } from "react";
import { api, fmtDate, resourceUrl, send, type Resource } from "@/lib/api";
import { Card, Empty, ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

function kind(mime: string) {
  if (mime.startsWith("image/")) return "Imagen";
  if (mime.startsWith("video/")) return "Video";
  if (mime.startsWith("audio/")) return "Audio";
  if (mime === "application/pdf") return "PDF";
  return "Documento";
}

export default function RecursosPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<Resource[]>("/api/resources");
  const [run, busy, actionError] = useAction();
  const fileRef = useRef<HTMLInputElement>(null);

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    await run(() => api("/api/resources", { method: "POST", body: form }));
    reload();
  }

  async function remove(r: Resource) {
    if (!confirm(`¿Eliminar «${r.name}»?`)) return;
    await run(() => send(`/api/resources/${r.id}`, "DELETE"));
    reload();
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Gestor de recursos: archivos que los asesores envían desde el chat." />
      <ConfigTabs />
      <AdminNotice />
      <Card
        title="Biblioteca"
        actions={
          isAdmin && (
            <>
              <button className="primary" disabled={busy} onClick={() => fileRef.current?.click()}>
                {busy ? "Subiendo…" : "Subir archivo"}
              </button>
              <input
                ref={fileRef}
                type="file"
                hidden
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
        <p className="small muted" style={{ marginTop: 0 }}>Catálogos, fichas técnicas, listas de precios o imágenes. Máximo 16 MB (límite de WhatsApp).</p>
        <ErrorBox error={error || actionError} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>La biblioteca está vacía.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Nombre</th><th>Tipo</th><th className="num">Tamaño</th><th>Subido</th><th /></tr></thead>
              <tbody>
                {data.map((r) => (
                  <tr key={r.id}>
                    <td className="strong">{r.name}</td>
                    <td className="small">{kind(r.mime)}</td>
                    <td className="num">{Math.max(1, Math.round(r.size / 1024)).toLocaleString("es")} KB</td>
                    <td className="small nowrap">{fmtDate(r.created_at)}</td>
                    <td className="nowrap">
                      <div className="inline">
                        <a href={resourceUrl(r.id)} target="_blank" rel="noreferrer">Ver</a>
                        {isAdmin && <button className="danger" onClick={() => remove(r)} disabled={busy}>Eliminar</button>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
