"use client";

import { Fragment, useState } from "react";
import { api, fmtDate, fmtDateTime, send, type Channel, type Template } from "@/lib/api";
import {
  HOOK_ACTIONS,
  MAPS_GROUPS,
  PARAM_TYPES,
  type HookAction,
  type HookParam,
  type HookRun,
  type InboundWebhook,
} from "@/lib/agent-config-types";
import { useMe } from "@/components/Shell";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";

type Group = { id: number; name: string };
type Flow = { id: number; name: string; status: string };
type HookForm = {
  name: string;
  action: HookAction;
  channel_id: number | null;
  template_name: string | null;
  template_language: string | null;
  flow_id: number | null;
  params: HookParam[];
  options: InboundWebhook["options"];
};

const STATUS: Record<InboundWebhook["status"], [string, "ok" | "warn" | "neutral"]> = {
  active: ["Activo", "ok"],
  paused: ["Pausado", "warn"],
  draft: ["Borrador", "neutral"],
};
const RUN_TONE: Record<HookRun["status"], "ok" | "bad" | "warn" | "neutral"> = {
  succeeded: "ok",
  failed: "bad",
  rejected: "bad",
  duplicate: "neutral",
};
const EMPTY_FORM: HookForm = {
  name: "",
  action: "send_template",
  channel_id: null,
  template_name: null,
  template_language: null,
  flow_id: null,
  params: [{ name: "telefono", label: "Teléfono del cliente", type: "phone", required: true, example: "3001234567", maps_to: "recipient.phone" }],
  options: { bot: "keep", tags: [] },
};

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* sin permiso de portapapeles */
  }
}

export default function InboundWebhooksPage() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const hooks = useApi<InboundWebhook[]>("/api/inbound-webhooks");
  const [editing, setEditing] = useState<{ id?: number; form: HookForm } | null>(null);
  const [token, setToken] = useState<{ name: string; token: string; url: string } | null>(null);
  const [runsFor, setRunsFor] = useState<InboundWebhook | null>(null);
  const [run, busy, error] = useAction();

  async function act(h: InboundWebhook, path: string) {
    const r = await run(() => send<InboundWebhook>(`/api/inbound-webhooks/${h.id}/${path}`, "POST", {}));
    if (r?.token) setToken({ name: r.name, token: r.token, url: r.url });
    if (r) await hooks.reload();
  }
  async function remove(h: InboundWebhook) {
    if (!confirm(`¿Eliminar el webhook «${h.name}»? Los sistemas que lo llaman empezarán a recibir 404.`)) return;
    if (await run(() => send(`/api/inbound-webhooks/${h.id}`, "DELETE"))) await hooks.reload();
  }

  return (
    <>
      <PageHeader
        title="Webhooks entrantes"
        subtitle="URLs que llaman tus otros sistemas (agenda de taller, DMS, ERP) para enviar plantillas, iniciar flujos o crear citas y oportunidades."
        actions={isAdmin ? <button className="primary" onClick={() => setEditing({ form: { ...EMPTY_FORM } })}>+ Nuevo webhook</button> : undefined}
      />
      <ErrorBox error={error || hooks.error} />
      <Card>
        {hooks.loading && !hooks.data ? <Loading /> : (hooks.data ?? []).length === 0 ? (
          <Empty>No hay webhooks entrantes. Crea uno para que tu sistema de citas confirme por WhatsApp.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>URL</th><th>Nombre</th><th>Estado</th><th className="num">Ejecuciones</th><th className="num">Exitosas</th>
                  <th className="num">Erróneas</th><th>Parámetros</th><th>Creador</th><th>F. Publicación</th><th>Acciones</th>
                </tr>
              </thead>
              <tbody>
                {(hooks.data ?? []).map((h) => {
                  const [label, tone] = STATUS[h.status];
                  return (
                    <tr key={h.id}>
                      <td><button className="link small" title={h.url} onClick={() => copy(h.url)}>Copiar URL</button></td>
                      <td><strong>{h.name}</strong><div className="muted small">{HOOK_ACTIONS.find(([a]) => a === h.action)?.[1]}</div></td>
                      <td><Badge tone={tone}>{label}</Badge></td>
                      <td className="num">{h.executions.toLocaleString("es")}</td>
                      <td className="num">{h.succeeded.toLocaleString("es")}</td>
                      <td className="num">{h.failed.toLocaleString("es")}</td>
                      <td>{h.params_count} parámetros</td>
                      <td>{h.created_by?.name ?? "—"}</td>
                      <td>{h.published_at ? fmtDate(h.published_at) : "—"}</td>
                      <td>
                        <div className="inline" style={{ flexWrap: "wrap" }}>
                          <button className="link small" onClick={() => setRunsFor(h)}>Historial</button>
                          {isAdmin && (
                            <>
                              <button className="link small" onClick={() => setEditing({ id: h.id, form: {
                                name: h.name, action: h.action, channel_id: h.channel_id, template_name: h.template_name,
                                template_language: h.template_language, flow_id: h.flow_id, params: h.params, options: h.options } })}>Editar</button>
                              {h.status === "active"
                                ? <button className="link small" onClick={() => act(h, "pause")}>Pausar</button>
                                : <button className="link small" onClick={() => act(h, "publish")}>Publicar</button>}
                              <button className="link small" onClick={() => act(h, "duplicate")}>Duplicar</button>
                              <button className="link small" onClick={() => act(h, "rotate-token")}>Nuevo token</button>
                              <button className="link small danger" onClick={() => remove(h)}>Eliminar</button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {editing && (
        <HookEditor
          id={editing.id}
          initial={editing.form}
          busy={busy}
          onClose={() => setEditing(null)}
          onSaved={async (h) => {
            setEditing(null);
            if (h.token) setToken({ name: h.name, token: h.token, url: h.url });
            await hooks.reload();
          }}
        />
      )}
      {token && (
        <Modal title={`Token de «${token.name}»`} onClose={() => setToken(null)}>
          <p className="small">Cópialo ahora: <strong>no se vuelve a mostrar</strong>. Envíalo en la cabecera <code>X-Hook-Token</code>.</p>
          <div className="row"><code style={{ wordBreak: "break-all" }}>{token.token}</code><button onClick={() => copy(token.token)}>Copiar</button></div>
          <p className="small muted" style={{ marginTop: 8 }}>URL: {token.url}</p>
          <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{`curl -X POST '${token.url}' \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Hook-Token: ${token.token}' \\\n  -d '{"telefono": "3001234567"}'`}</pre>
        </Modal>
      )}
      {runsFor && <RunsDrawer hook={runsFor} onClose={() => setRunsFor(null)} />}
    </>
  );
}

function HookEditor({ id, initial, busy: outerBusy, onClose, onSaved }: {
  id?: number;
  initial: HookForm;
  busy: boolean;
  onClose: () => void;
  onSaved: (h: InboundWebhook) => void;
}) {
  const [form, setForm] = useState<HookForm>(initial);
  const [test, setTest] = useState<unknown>(null);
  const [run, busy, error] = useAction();
  const chans = useApi<{ channels: Channel[] }>("/api/channels");
  const templates = useApi<Template[]>("/api/templates");
  const flows = useApi<Flow[]>("/api/flows");
  const groups = useApi<Group[]>("/api/groups");
  const set = <K extends keyof HookForm>(k: K, v: HookForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const setParam = (i: number, p: HookParam) => set("params", form.params.map((x, j) => (j === i ? p : x)));
  const waChannels = (chans.data?.channels ?? []).filter((c) => !("provider" in c) || (c as { provider?: string }).provider === "whatsapp_cloud");

  async function loadTemplateParams(name: string, language: string) {
    const channelId = form.channel_id ?? waChannels[0]?.id;
    if (!channelId) return;
    const params = await run(() => api<HookParam[]>(`/api/inbound-webhooks/template-params?channel_id=${channelId}&name=${encodeURIComponent(name)}&language=${encodeURIComponent(language)}`));
    if (params) set("params", params);
  }

  async function save() {
    const body = { ...form, params: form.params.map((p) => ({ ...p, maps_to: p.maps_to || null })) };
    const h = await run(() => id ? send<InboundWebhook>(`/api/inbound-webhooks/${id}`, "PUT", body) : send<InboundWebhook>("/api/inbound-webhooks", "POST", body));
    if (h) onSaved(h);
  }
  async function dryRun() {
    if (!id) return;
    setTest(await run(() => send(`/api/inbound-webhooks/${id}/test`, "POST", {})));
  }

  return (
    <Modal
      wide
      title={id ? `Editar «${initial.name}»` : "Nuevo webhook entrante"}
      onClose={onClose}
      footer={
        <div className="row">
          {id && <button onClick={dryRun} disabled={busy}>Probar con los ejemplos</button>}
          <button className="primary" disabled={busy || outerBusy || !form.name.trim()} onClick={save}>{id ? "Guardar" : "Crear (borrador)"}</button>
        </div>
      }
    >
      <div className="form">
        <div className="grid2">
          <Field label="Nombre"><input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Confirmación Cita V.5" /></Field>
          <Field label="Acción">
            <select value={form.action} onChange={(e) => set("action", e.target.value as HookAction)}>
              {HOOK_ACTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </Field>
          <Field label="Número de WhatsApp">
            <select value={form.channel_id ?? ""} onChange={(e) => set("channel_id", e.target.value ? Number(e.target.value) : null)}>
              <option value="">El principal</option>
              {waChannels.map((c) => <option key={c.id} value={c.id}>{c.name} {c.display_phone ?? ""}</option>)}
            </select>
          </Field>
          {form.action === "send_template" && (
            <Field label="Plantilla" hint="Al elegirla se crean los parámetros de sus variables.">
              <select
                value={form.template_name ? `${form.template_name}|${form.template_language ?? ""}` : ""}
                onChange={(e) => {
                  const [name, language] = e.target.value.split("|");
                  setForm((f) => ({ ...f, template_name: name || null, template_language: language || null }));
                  if (name) loadTemplateParams(name, language);
                }}
              >
                <option value="">Elige una plantilla aprobada</option>
                {(templates.data ?? []).filter((t) => t.status === "APPROVED").map((t) => (
                  <option key={`${t.name}|${t.language}`} value={`${t.name}|${t.language}`}>{t.name} ({t.language}) · {t.variables.length} variables</option>
                ))}
              </select>
            </Field>
          )}
          {form.action === "start_flow" && (
            <Field label="Flujo">
              <select value={form.flow_id ?? ""} onChange={(e) => set("flow_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">Elige un flujo</option>
                {(flows.data ?? []).map((f) => <option key={f.id} value={f.id}>{f.name}{f.status !== "active" ? " (inactivo)" : ""}</option>)}
              </select>
            </Field>
          )}
        </div>

        <h4 style={{ margin: "8px 0 0" }}>Parámetros ({form.params.length})</h4>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Nombre</th><th>Tipo</th><th>Obligatorio</th><th>Ejemplo</th><th>Se guarda en</th><th /></tr></thead>
            <tbody>
              {form.params.map((p, i) => (
                <tr key={i}>
                  <td><input aria-label="Nombre del parámetro" value={p.name} onChange={(e) => setParam(i, { ...p, name: e.target.value.replace(/[^\w]/g, "") })} style={{ width: 130 }} /></td>
                  <td>
                    <select aria-label="Tipo" value={p.type} onChange={(e) => setParam(i, { ...p, type: e.target.value as HookParam["type"] })}>
                      {PARAM_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </td>
                  <td><input aria-label="Obligatorio" type="checkbox" checked={p.required} onChange={(e) => setParam(i, { ...p, required: e.target.checked })} /></td>
                  <td><input aria-label="Ejemplo" value={p.example ?? ""} onChange={(e) => setParam(i, { ...p, example: e.target.value || null })} style={{ width: 130 }} /></td>
                  <td>
                    <select aria-label="Destino" value={p.maps_to ?? ""} onChange={(e) => setParam(i, { ...p, maps_to: e.target.value || null })}>
                      <option value="">Solo para el flujo / registro</option>
                      {p.maps_to?.startsWith("template.") && <option value={p.maps_to}>Variable de la plantilla ({p.maps_to.split(".").slice(1).join(".")})</option>}
                      {p.maps_to?.startsWith("field:") && <option value={p.maps_to}>Campo {p.maps_to.slice(6)}</option>}
                      {MAPS_GROUPS.map((g) => (
                        <optgroup key={g.label} label={g.label}>
                          {g.options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                        </optgroup>
                      ))}
                      <optgroup label="Flujo">
                        <option value={`flow.var.${p.name}`}>Variable del flujo «{p.name}»</option>
                      </optgroup>
                    </select>
                  </td>
                  <td><button className="icon" aria-label="Quitar" onClick={() => set("params", form.params.filter((_x, j) => j !== i))}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div><button onClick={() => set("params", [...form.params, { name: `param_${form.params.length + 1}`, type: "text", required: false, maps_to: null }])}>+ Agregar parámetro</button></div>

        <h4 style={{ margin: "8px 0 0" }}>Opciones</h4>
        <div className="grid2">
          <Field label="Asignar al grupo">
            <select value={form.options.assign_group_id ?? ""} onChange={(e) => set("options", { ...form.options, assign_group_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">Sin cambio</option>
              {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
          </Field>
          <Field label="Bot">
            <select value={form.options.bot ?? "keep"} onChange={(e) => set("options", { ...form.options, bot: e.target.value as "on" | "off" | "keep" })}>
              <option value="keep">Sin cambio</option><option value="on">Encender (lo atiende el bot)</option><option value="off">Apagar (lo atiende un asesor)</option>
            </select>
          </Field>
          <Field label="Etiquetas" hint="Separadas por coma.">
            <input value={(form.options.tags ?? []).join(", ")} onChange={(e) => set("options", { ...form.options, tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })} />
          </Field>
          <Field label="Evitar duplicados (minutos)" hint="Mismos parámetros dentro de este tiempo = no se repite. También se respeta la cabecera Idempotency-Key.">
            <input type="number" min={0} value={form.options.dedupe_minutes ?? ""} onChange={(e) => set("options", { ...form.options, dedupe_minutes: e.target.value ? Number(e.target.value) : null })} />
          </Field>
        </div>
        {form.action === "send_template" && (
          <Toggle checked={Boolean(form.options.prefer_free_form)} onChange={(v) => set("options", { ...form.options, prefer_free_form: v })}
            label="Preferir mensaje libre si la ventana de 24 h está abierta (sin costo, con la optimización de costos del agente)" />
        )}
        <ErrorBox error={error} />
        {test != null && <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(test, null, 2)}</pre>}
      </div>
    </Modal>
  );
}

function RunsDrawer({ hook, onClose }: { hook: InboundWebhook; onClose: () => void }) {
  const runs = useApi<{ items: HookRun[] }>(`/api/inbound-webhooks/${hook.id}/runs?limit=100`);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Modal wide title={`Historial · ${hook.name}`} onClose={onClose}>
      {runs.loading && !runs.data ? <Loading /> : (runs.data?.items ?? []).length === 0 ? <Empty>Aún no hay ejecuciones.</Empty> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Fecha</th><th>Estado</th><th>HTTP</th><th>Cliente</th><th className="num">Latencia</th><th>Error</th><th /></tr></thead>
            <tbody>
              {(runs.data?.items ?? []).map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td>{fmtDateTime(r.created_at)}</td>
                    <td><Badge tone={RUN_TONE[r.status]}>{r.status}</Badge></td>
                    <td>{r.http_status}</td>
                    <td>{r.contact_id ?? "—"}</td>
                    <td className="num">{r.latency_ms != null ? `${r.latency_ms} ms` : "—"}</td>
                    <td className="small">{r.error ?? ""}</td>
                    <td><button className="link small" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "Ocultar" : "Ver"}</button></td>
                  </tr>
                  {open === r.id && (
                    <tr>
                      <td colSpan={7}><pre className="small" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ payload: r.payload, result: r.result }, null, 2)}</pre></td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
