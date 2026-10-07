"use client";

import { useState } from "react";
import { API_URL, fmtDateTime, fmtNum, send, timeAgo } from "@/lib/api";
import type { ApiKeyIn, ApiKeyOut, ScopeOption } from "@/lib/api-keys-types";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { CodeBlock } from "@/components/attribution/common";

/** 402: el plan de la empresa no incluye la API (Profesional y Enterprise sí). */
const planBlocked = (error: string | null | undefined) =>
  error && /plan/i.test(error) ? "Tu plan no incluye la API pública. Actualízalo en Configuraciones › Plan y facturación." : null;

const EMPTY: ApiKeyIn = { name: "", scopes: [], rate_limit_per_min: 120, expires_at: null };

/** Alcances sugeridos por conector. */
const PRESETS: { label: string; scopes: string[] }[] = [
  { label: "Zapier / Make / n8n", scopes: ["contacts:read", "contacts:write", "conversations:read", "messages:send", "deals:write", "webhooks:manage"] },
  { label: "Solo lectura", scopes: ["contacts:read", "conversations:read", "messages:read", "deals:read", "reports:read"] },
  { label: "Envío de mensajes", scopes: ["contacts:write", "messages:send"] },
];

export default function ApiKeysPage() {
  const isAdmin = useIsAdmin();
  const keys = useApi<ApiKeyOut[]>(isAdmin ? "/api/api-keys" : null);
  const scopes = useApi<ScopeOption[]>(isAdmin ? "/api/api-keys/scopes" : null);
  const [editing, setEditing] = useState<{ id: number | null; form: ApiKeyIn } | null>(null);
  const [revealed, setRevealed] = useState<ApiKeyOut | null>(null);
  const [run, busy, error] = useAction();

  const save = async () => {
    if (!editing) return;
    const body = { ...editing.form, expires_at: editing.form.expires_at ? new Date(editing.form.expires_at).toISOString() : null };
    const out = await run(() =>
      editing.id ? send<ApiKeyOut>(`/api/api-keys/${editing.id}`, "PUT", body) : send<ApiKeyOut>("/api/api-keys", "POST", body),
    );
    if (out) {
      setEditing(null);
      if (out.key) setRevealed(out);
      keys.reload();
    }
  };
  const rotate = async (k: ApiKeyOut) => {
    if (!confirm(`¿Rotar «${k.name}»? La llave actual deja de funcionar de inmediato.`)) return;
    const out = await run(() => send<ApiKeyOut>(`/api/api-keys/${k.id}/rotate`, "POST"));
    if (out) {
      setRevealed(out);
      keys.reload();
    }
  };
  const revoke = async (k: ApiKeyOut) => {
    if (!confirm(`¿Revocar «${k.name}»? Las integraciones que la usan dejarán de funcionar y sus webhooks se desactivan.`)) return;
    if (await run(() => send(`/api/api-keys/${k.id}/revoke`, "POST"))) keys.reload();
  };
  const toggleScope = (s: string) =>
    editing &&
    setEditing({
      ...editing,
      form: {
        ...editing.form,
        scopes: editing.form.scopes.includes(s) ? editing.form.scopes.filter((x) => x !== s) : [...editing.form.scopes, s],
      },
    });

  const blocked = planBlocked(keys.error);
  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="API pública: conecta tu CRM, ERP, Zapier, Make o n8n con llaves de API y webhooks firmados."
      />
      <ConfigTabs />
      <AdminNotice />
      {isAdmin && (
        <>
          <Card title="Cómo conectarse">
            <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
              <li>Crea una llave con solo los alcances que la integración necesita. Se muestra una sola vez.</li>
              <li>
                Envía <code>Authorization: Bearer wak_live_…</code> a <code>{API_URL}/v1/…</code>. Referencia interactiva en{" "}
                <a href={`${API_URL}/v1/docs`} target="_blank" rel="noreferrer">/v1/docs</a>.
              </li>
              <li>
                Zapier, Make y n8n: usa <code>GET /v1/me</code> para probar la conexión y <code>POST /v1/webhooks</code> (REST Hooks)
                para recibir eventos firmados con <code>X-Signature-256</code>.
              </li>
            </ol>
            <div style={{ marginTop: 10 }}>
              <CodeBlock label="Ejemplo" code={`curl ${API_URL}/v1/contacts \\\n  -H "Authorization: Bearer wak_live_…" \\\n  -H "Idempotency-Key: crm-123" \\\n  -H "Content-Type: application/json" \\\n  -d '{"phone": "573001234567", "name": "Ana", "tags": ["crm"]}'`} />
            </div>
          </Card>

          <Card
            title="Llaves de API"
            actions={!blocked && <button className="primary" onClick={() => setEditing({ id: null, form: EMPTY })}>+ Nueva llave</button>}
          >
            {blocked ? (
              <div className="error-box" style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>{blocked}</div>
            ) : keys.error ? (
              <ErrorBox error={keys.error} />
            ) : !keys.data ? (
              <Loading />
            ) : keys.data.length === 0 ? (
              <Empty>Aún no hay llaves. Crea una para tu primera integración.</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Nombre</th>
                      <th>Llave</th>
                      <th>Alcances</th>
                      <th className="num">Uso (30 días)</th>
                      <th>Último uso</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {keys.data.map((k) => {
                      const expired = k.expires_at && new Date(k.expires_at) < new Date();
                      return (
                        <tr key={k.id} style={k.revoked_at ? { opacity: 0.6 } : undefined}>
                          <td>
                            <span className="strong">{k.name}</span>{" "}
                            {k.revoked_at ? <Badge tone="bad">Revocada</Badge> : expired ? <Badge tone="warn">Vencida</Badge> : <Badge tone="ok">Activa</Badge>}
                            <div className="muted small">
                              {fmtNum(k.rate_limit_per_min)}/min
                              {k.expires_at && ` · vence ${fmtDateTime(k.expires_at)}`}
                              {k.webhooks > 0 && ` · ${k.webhooks} webhook${k.webhooks > 1 ? "s" : ""}`}
                            </div>
                          </td>
                          <td><code>{k.prefix}_…</code></td>
                          <td className="small">{k.scopes.join(", ")}</td>
                          <td className="num">{fmtNum(k.requests_30d)}</td>
                          <td className="small">{k.last_used_at ? `${timeAgo(k.last_used_at)}${k.last_used_ip ? ` · ${k.last_used_ip}` : ""}` : "Nunca"}</td>
                          <td>
                            {!k.revoked_at && (
                              <span className="inline">
                                <button className="small" onClick={() => setEditing({ id: k.id, form: { name: k.name, scopes: k.scopes, rate_limit_per_min: k.rate_limit_per_min, expires_at: k.expires_at ? k.expires_at.slice(0, 16) : null } })}>Editar</button>
                                <button className="small" onClick={() => rotate(k)} disabled={busy}>Rotar</button>
                                <button className="small danger" onClick={() => revoke(k)} disabled={busy}>Revocar</button>
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {error && <ErrorBox error={error} />}
          </Card>
        </>
      )}

      {editing && (
        <Modal
          title={editing.id ? "Editar llave" : "Nueva llave de API"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button onClick={() => setEditing(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !editing.form.name.trim() || editing.form.scopes.length === 0} onClick={save}>
                {editing.id ? "Guardar" : "Crear llave"}
              </button>
            </>
          }
        >
          <div className="stack">
            <Field label="Nombre" hint="Para reconocerla: «Zapier ventas», «ERP», «Landing».">
              <input autoFocus value={editing.form.name} onChange={(e) => setEditing({ ...editing, form: { ...editing.form, name: e.target.value } })} />
            </Field>
            <div className="field">
              <span>Alcances</span>
              <div className="inline small">
                {PRESETS.map((p) => (
                  <button key={p.label} className="link small" onClick={() => setEditing({ ...editing, form: { ...editing.form, scopes: p.scopes } })}>
                    {p.label}
                  </button>
                ))}
              </div>
              <div className="grid2" style={{ gap: 6 }}>
                {(scopes.data ?? []).map((s) => (
                  <label key={s.key} className="inline small">
                    <input type="checkbox" checked={editing.form.scopes.includes(s.key)} onChange={() => toggleScope(s.key)} />
                    <span><code>{s.key}</code> · {s.label}</span>
                  </label>
                ))}
              </div>
            </div>
            <div className="grid2">
              <Field label="Límite por minuto" hint="Al superarlo responde 429 con Retry-After.">
                <input type="number" min={1} max={6000} value={editing.form.rate_limit_per_min}
                  onChange={(e) => setEditing({ ...editing, form: { ...editing.form, rate_limit_per_min: Number(e.target.value) || 1 } })} />
              </Field>
              <Field label="Vence (opcional)">
                <input type="datetime-local" value={editing.form.expires_at ?? ""}
                  onChange={(e) => setEditing({ ...editing, form: { ...editing.form, expires_at: e.target.value || null } })} />
              </Field>
            </div>
            {error && <ErrorBox error={error} />}
          </div>
        </Modal>
      )}

      {revealed?.key && (
        <Modal title="Copia la llave ahora" onClose={() => setRevealed(null)} footer={<button className="primary" onClick={() => setRevealed(null)}>Ya la guardé</button>}>
          <div className="stack">
            <p className="small">
              Esta es la única vez que se muestra «{revealed.name}». Guárdala en el gestor de secretos de tu integración; si la pierdes, rótala.
            </p>
            <CodeBlock code={revealed.key} />
          </div>
        </Modal>
      )}
    </>
  );
}
