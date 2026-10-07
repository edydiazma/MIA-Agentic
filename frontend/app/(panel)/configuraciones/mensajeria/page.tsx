"use client";

import { useState } from "react";
import { send, type Group, type Resource, type Template } from "@/lib/api";
import { QUICK_VARIABLES, type QuickReplyV2 } from "@/lib/productivity-types";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

type Draft = {
  id?: number;
  shortcut: string;
  text: string;
  title: string;
  category: string;
  resource_ids: number[];
  group_ids: number[];
  is_active: boolean;
};
const EMPTY_DRAFT: Draft = { shortcut: "", text: "", title: "", category: "", resource_ids: [], group_ids: [], is_active: true };

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "neutral"> = {
  APPROVED: "ok",
  PENDING: "warn",
  PAUSED: "warn",
  REJECTED: "bad",
  DISABLED: "bad",
};

export default function MensajeriaPage() {
  const isAdmin = useIsAdmin();
  const qr = useApi<QuickReplyV2[]>("/api/quick-replies?include_inactive=true");
  const groups = useApi<Group[]>("/api/groups");
  const resources = useApi<Resource[]>("/api/resources");
  const groupName = new Map((groups.data ?? []).map((g) => [g.id, g.name]));
  const tpl = useApi<Template[]>("/api/templates");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  async function save() {
    if (!draft) return;
    const body = { ...draft, id: undefined }; // el id va en la URL
    const ok = await run(() => (draft.id ? send(`/api/quick-replies/${draft.id}`, "PUT", body) : send("/api/quick-replies", "POST", body)));
    if (ok) {
      setDraft(null);
      qr.reload();
    }
  }

  async function remove(q: QuickReplyV2) {
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
        actions={isAdmin && <button className="primary" onClick={() => { setActionError(null); setDraft({ ...EMPTY_DRAFT }); }}>Nueva respuesta</button>}
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          Los asesores las insertan escribiendo <code>/atajo</code> (o parte del título o la categoría) en el chat. Pueden llevar
          adjuntos, limitarse a ciertos grupos y usar variables como <code>{"{{client_first_name}}"}</code>.
        </p>
        <ErrorBox error={qr.error} />
        {qr.loading && !qr.data ? (
          <Loading />
        ) : !qr.data?.length ? (
          <Empty>Sin respuestas rápidas.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Atajo</th><th>Categoría</th><th>Texto</th><th>Grupos</th><th className="num">Usos</th><th /></tr></thead>
              <tbody>
                {qr.data.map((q) => (
                  <tr key={q.id} style={q.is_active ? undefined : { opacity: 0.55 }}>
                    <td>
                      <code>/{q.shortcut}</code>
                      {q.title && <div className="small">{q.title}</div>}
                      {!q.is_active && <Badge tone="neutral">Inactiva</Badge>}
                    </td>
                    <td className="small">{q.category ?? "—"}</td>
                    <td style={{ whiteSpace: "pre-wrap" }}>
                      {q.text}
                      {q.attachments.length > 0 && (
                        <div className="small muted">📎 {q.attachments.map((a) => a.name).join(", ")}</div>
                      )}
                    </td>
                    <td className="small">
                      {q.group_ids.length ? q.group_ids.map((g) => groupName.get(g) ?? `#${g}`).join(", ") : "Todos"}
                    </td>
                    <td className="num">{q.usage_count}</td>
                    <td className="nowrap">
                      {isAdmin && (
                        <div className="inline">
                          <button
                            onClick={() => {
                              setActionError(null);
                              setDraft({
                                id: q.id, shortcut: q.shortcut, text: q.text, title: q.title ?? "", category: q.category ?? "",
                                resource_ids: q.resource_ids, group_ids: q.group_ids, is_active: q.is_active,
                              });
                            }}
                          >
                            Editar
                          </button>
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
          <Field label="Título (opcional)">
            <input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
          </Field>
          <Field label="Categoría (opcional)" hint="Agrupa las respuestas en el buscador del chat, p. ej. Posventa">
            <input value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value })} />
          </Field>
          <Field label="Texto">
            <textarea rows={5} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          </Field>
          <div className="inline small" aria-label="Variables">
            <span className="muted">Insertar variable:</span>
            {QUICK_VARIABLES.map((v) => (
              <button key={v.key} type="button" className="link small" title={v.label}
                onClick={() => setDraft({ ...draft, text: `${draft.text}${v.key}` })}>
                {v.key}
              </button>
            ))}
          </div>
          <Field label="Adjuntos (Gestor de recursos)">
            <select multiple size={Math.min(5, Math.max(2, resources.data?.length ?? 2))} value={draft.resource_ids.map(String)}
              onChange={(e) => setDraft({ ...draft, resource_ids: [...e.target.selectedOptions].map((o) => Number(o.value)) })}>
              {(resources.data ?? []).map((r) => (
                <option key={r.id} value={r.id}>{r.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Disponible para los grupos" hint="Sin selección = todos los grupos">
            <select multiple size={Math.min(5, Math.max(2, groups.data?.length ?? 2))} value={draft.group_ids.map(String)}
              onChange={(e) => setDraft({ ...draft, group_ids: [...e.target.selectedOptions].map((o) => Number(o.value)) })}>
              {(groups.data ?? []).map((g) => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
          </Field>
          <label className="inline small">
            <input type="checkbox" checked={draft.is_active} onChange={(e) => setDraft({ ...draft, is_active: e.target.checked })} />
            Activa
          </label>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
