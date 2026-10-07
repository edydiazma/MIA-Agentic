"use client";

import { useState } from "react";
import { api, fmtDateTime, send, type OutboundWebhook } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, copy, useIsAdmin } from "@/components/config/common";

type Draft = { id?: number; name: string; url: string; events: string[]; active: boolean };

function stateBadge(w: OutboundWebhook) {
  if (!w.active) return <Badge tone="bad">Desactivado</Badge>;
  if (w.consecutive_failures) return <Badge tone="warn">{w.consecutive_failures} fallos</Badge>;
  return <Badge tone="ok">Activo</Badge>;
}

/** El secreto vive en Vault: solo se pide al backend cuando el administrador lo revela. */
function Secret({ id, onRotated }: { id: number; onRotated: () => void }) {
  const [value, setValue] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function reveal() {
    try {
      setValue((await api<{ secret: string }>(`/api/outbound-webhooks/${id}/secret`)).secret);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  async function rotate() {
    if (!confirm("¿Generar un secreto nuevo? El sistema que recibe el webhook deberá actualizarlo.")) return;
    const r = await send<{ secret: string }>(`/api/outbound-webhooks/${id}/rotate-secret`, "POST");
    setValue(r.secret);
    onRotated();
  }
  return (
    <span className="inline">
      <code className="small">{value ?? "••••••••••••"}</code>
      {value ? (
        <>
          <button className="link small" onClick={() => setValue(null)}>Ocultar</button>
          <button className="link small" onClick={() => copy(value)}>Copiar</button>
        </>
      ) : (
        <button className="link small" onClick={reveal}>Mostrar</button>
      )}
      <button className="link small" onClick={rotate}>Rotar</button>
      {err && <span className="error small">{err}</span>}
    </span>
  );
}

export default function WebhooksPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<OutboundWebhook[]>(isAdmin ? "/api/outbound-webhooks" : null);
  const events = useApi<string[]>("/api/webhooks/events").data ?? [];
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const [testResult, setTestResult] = useState<Record<number, string>>({});

  async function save() {
    if (!draft) return;
    const { id, ...body } = draft;
    const ok = await run(() =>
      id ? send(`/api/outbound-webhooks/${id}`, "PUT", body) : send("/api/outbound-webhooks", "POST", body),
    );
    if (ok) {
      setDraft(null);
      reload();
    }
  }

  async function test(w: OutboundWebhook) {
    const r = await run(() => send<OutboundWebhook>(`/api/outbound-webhooks/${w.id}/test`, "POST"));
    if (r)
      setTestResult((t) => ({
        ...t,
        [w.id]: r.last_error ? `Error: ${r.last_error}` : `Respuesta HTTP ${r.last_status ?? "—"}`,
      }));
    reload();
  }

  async function remove(w: OutboundWebhook) {
    if (!confirm(`¿Eliminar el webhook «${w.name}»?`)) return;
    await run(() => send(`/api/outbound-webhooks/${w.id}`, "DELETE"));
    reload();
  }

  return (
    <>
      <PageHeader
        title="Webhooks"
        subtitle="Envía los eventos de la plataforma a tus sistemas (CRM, BI, automatizaciones)."
        actions={isAdmin && (
          <button className="primary" onClick={() => { setActionError(null); setDraft({ name: "", url: "https://", events: [], active: true }); }}>
            Nuevo webhook
          </button>
        )}
      />
      <AdminNotice />
      <ErrorBox error={error || (!draft ? actionError : null)} />

      {isAdmin && (loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Card><Empty>No hay webhooks configurados.</Empty></Card>
      ) : (
        <Card>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Nombre</th><th>URL</th><th>Eventos</th><th>Estado</th><th>Última entrega</th><th>Secreto</th><th /></tr>
              </thead>
              <tbody>
                {data.map((w) => (
                  <tr key={w.id}>
                    <td className="strong">{w.name}</td>
                    <td className="small" style={{ overflowWrap: "anywhere" }}>{w.url}</td>
                    <td className="small">{w.events.length ? w.events.join(", ") : "Todos"}</td>
                    <td>
                      {stateBadge(w)}
                      {w.last_error && <div className="small error">{w.last_error}</div>}
                    </td>
                    <td className="small nowrap">
                      {fmtDateTime(w.last_delivery_at)}
                      {w.last_status != null && <div className="muted">HTTP {w.last_status}</div>}
                      {testResult[w.id] && <div className="muted">{testResult[w.id]}</div>}
                    </td>
                    <td><Secret id={w.id} onRotated={reload} /></td>
                    <td className="nowrap">
                      <div className="inline">
                        <button onClick={() => test(w)} disabled={busy || !w.active}>Probar</button>
                        <button onClick={() => { setActionError(null); setDraft({ id: w.id, name: w.name, url: w.url, events: w.events, active: w.active }); }}>Editar</button>
                        <button className="danger" onClick={() => remove(w)} disabled={busy}>Eliminar</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}

      <Card title="Cómo verificar los eventos">
        <p className="small muted" style={{ marginTop: 0 }}>
          Cada evento llega como <code>POST</code> con cuerpo JSON <code>{"{ event, data, sent_at }"}</code> y el encabezado{" "}
          <code>X-Signature-256: sha256=HMAC_SHA256(secreto, cuerpo)</code>. Calcula el HMAC del cuerpo crudo con el secreto
          del webhook y compáralo antes de procesar. Tras 10 fallos seguidos el webhook se desactiva y aparece una alerta en
          Inicio.
        </p>
        <pre className="small" style={{ background: "var(--panel-2)", padding: 12, borderRadius: 8, overflowX: "auto" }}>
{`import hmac, hashlib
esperado = "sha256=" + hmac.new(SECRETO.encode(), cuerpo_crudo, hashlib.sha256).hexdigest()
valido = hmac.compare_digest(esperado, request.headers["X-Signature-256"])`}
        </pre>
      </Card>

      {draft && (
        <Modal
          title={draft.id ? "Editar webhook" : "Nuevo webhook"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.name.trim()}>Guardar</button>
            </>
          }
        >
          <Field label="Nombre"><input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Field>
          <Field label="URL" hint="Debe usar https://">
            <input value={draft.url} onChange={(e) => setDraft({ ...draft, url: e.target.value })} />
          </Field>
          <Field label="Eventos" hint="Si no marcas ninguno, se envían todos.">
            <div className="stack" style={{ gap: 4 }}>
              {events.map((ev) => (
                <label key={ev} className="inline small">
                  <input
                    type="checkbox"
                    checked={draft.events.includes(ev)}
                    onChange={(e) =>
                      setDraft({ ...draft, events: e.target.checked ? [...draft.events, ev] : draft.events.filter((x) => x !== ev) })
                    }
                  />
                  <code>{ev}</code>
                </label>
              ))}
            </div>
          </Field>
          <Toggle checked={draft.active} onChange={(v) => setDraft({ ...draft, active: v })} label={draft.active ? "Activo" : "Inactivo"} />
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
