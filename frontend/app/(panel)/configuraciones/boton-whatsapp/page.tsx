"use client";

import { useState } from "react";
import { fmtNum, send, type Channel } from "@/lib/api";
import type { WaLink } from "@/lib/attribution-types";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { CodeBlock } from "@/components/attribution/common";

type Hours = { days: number[]; from: string; to: string } | null;
type WidgetAgent = {
  name: string;
  role: string | null;
  photo_url: string | null;
  prefill: string | null;
  channel_id: number | null;
  link_id: number | null;
  hours: Hours;
};
type WidgetConfig = {
  mode: "single" | "multi";
  agents: WidgetAgent[];
  button: { text: string | null; color: string; position: "left" | "right" };
  title: string;
  greeting: string | null;
  show_on: { paths_include: string[]; paths_exclude: string[]; delay_s: number; mobile: boolean; desktop: boolean };
  allowed_domains: string[];
  timezone: string;
};
type Widget = {
  id: number;
  name: string;
  key: string;
  config: WidgetConfig;
  link_id: number | null;
  is_active: boolean;
  impressions: number;
  clicks: number;
  ctr_pct: number | null;
  embed: string;
  script_url: string;
};
type Draft = { id?: number; name: string; is_active: boolean; link_id: number | null; config: WidgetConfig };

const DAY_LABELS = ["L", "M", "X", "J", "V", "S", "D"];
const NEW_AGENT: WidgetAgent = { name: "", role: null, photo_url: null, prefill: null, channel_id: null, link_id: null, hours: null };
const DEFAULT_CONFIG: WidgetConfig = {
  mode: "single",
  agents: [{ ...NEW_AGENT, name: "Ventas", role: "Te respondemos en minutos" }],
  button: { text: "Escríbenos", color: "#25d366", position: "right" },
  title: "¿Hablamos por WhatsApp?",
  greeting: "Elige con quién quieres hablar",
  show_on: { paths_include: [], paths_exclude: [], delay_s: 0, mobile: true, desktop: true },
  allowed_domains: [],
  timezone: "America/Bogota",
};

const lines = (v: string) => v.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);

/** Vista previa fiel del botón (mismo aspecto que /b/{key}.js). */
function Preview({ c }: { c: WidgetConfig }) {
  const [open, setOpen] = useState(c.mode === "multi");
  const side = c.button.position === "left" ? { left: 16 } : { right: 16 };
  return (
    <div style={{ position: "relative", height: 380, background: "var(--panel-2)", borderRadius: 12, overflow: "hidden" }} aria-label="Vista previa">
      <div className="muted small" style={{ padding: 12 }}>Vista previa en tu sitio</div>
      {c.mode === "multi" && open && (
        <div style={{ position: "absolute", bottom: 84, ...side, width: 280, background: "#fff", borderRadius: 14,
          boxShadow: "0 8px 30px rgba(0,0,0,.25)", overflow: "hidden", color: "#1f2937", fontSize: 14 }}>
          <div style={{ background: c.button.color, color: "#fff", padding: 12 }}>
            <strong style={{ display: "block" }}>{c.title}</strong>
            {c.greeting && <span>{c.greeting}</span>}
          </div>
          <div style={{ padding: 8 }}>
            {c.agents.map((a, i) => (
              <div key={i} style={{ display: "flex", gap: 10, alignItems: "center", padding: 8 }}>
                <span style={{ width: 36, height: 36, borderRadius: "50%", background: "#e5e7eb", display: "grid", placeItems: "center", fontWeight: 700, overflow: "hidden" }}>
                  {a.photo_url ? <img src={a.photo_url} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} /> : (a.name || "?").charAt(0)}
                </span>
                <span>
                  {a.name || "Sin nombre"}
                  <small style={{ display: "block", color: "#6b7280" }}>{a.role}</small>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      <button type="button" onClick={() => setOpen((v) => !v)}
        style={{ position: "absolute", bottom: 16, ...side, background: c.button.color, color: "#fff", border: 0, borderRadius: 28,
          height: 52, padding: c.button.text ? "0 18px" : 0, width: c.button.text ? "auto" : 52, fontWeight: 600, display: "flex",
          alignItems: "center", gap: 8, justifyContent: "center", boxShadow: "0 4px 14px rgba(0,0,0,.25)" }}>
        <span aria-hidden="true" style={{ fontSize: 22 }}>🟢</span>
        {c.button.text}
      </button>
    </div>
  );
}

export default function WhatsAppButtonPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<Widget[]>("/api/wa-widgets");
  const links = useApi<WaLink[]>("/api/wa-links");
  const channels = useApi<{ channels: Channel[] }>("/api/channels");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [embed, setEmbed] = useState<Widget | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const waChannels = (channels.data?.channels ?? []).filter((c) => !c.provider || c.provider === "whatsapp_cloud");

  const cfg = draft?.config;
  const setCfg = (patch: Partial<WidgetConfig>) => draft && setDraft({ ...draft, config: { ...draft.config, ...patch } });
  const setAgent = (i: number, patch: Partial<WidgetAgent>) =>
    cfg && setCfg({ agents: cfg.agents.map((a, j) => (j === i ? { ...a, ...patch } : a)) });

  async function save() {
    if (!draft) return;
    const body = { name: draft.name, is_active: draft.is_active, link_id: draft.link_id, config: draft.config };
    const r = await run(() => (draft.id ? send<Widget>(`/api/wa-widgets/${draft.id}`, "PUT", body) : send<Widget>("/api/wa-widgets", "POST", body)));
    if (r) {
      setDraft(null);
      reload();
      if (!draft.id) setEmbed(r);
    }
  }

  async function remove(w: Widget) {
    if (!confirm(`¿Eliminar el botón «${w.name}»? Dejará de aparecer en los sitios donde esté pegado.`)) return;
    if (await run(() => send(`/api/wa-widgets/${w.id}`, "DELETE"))) reload();
  }

  return (
    <>
      <PageHeader
        title="Botón de WhatsApp"
        subtitle="Botón flotante para tu sitio web: uno o varios asesores, horario, páginas donde aparece y atribución de cada clic."
        actions={
          isAdmin && (
            <button className="primary" onClick={() => { setActionError(null); setDraft({ name: "Botón del sitio", is_active: true, link_id: null, config: DEFAULT_CONFIG }); }}>
              + Nuevo botón
            </button>
          )
        }
      />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={error ?? (!draft ? actionError : null)} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Card>
          <Empty>
            Aún no tienes botones. Crea uno, pega una línea de código en tu sitio y cada clic llega a WhatsApp atribuido a tu mensaje
            disparador.
          </Empty>
        </Card>
      ) : (
        <Card title="Botones">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Nombre</th>
                  <th>Asesores</th>
                  <th className="num">Impresiones</th>
                  <th className="num">Clics</th>
                  <th className="num">CTR</th>
                  <th>Estado</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((w) => (
                  <tr key={w.id}>
                    <td className="strong">{w.name}</td>
                    <td>{w.config.mode === "multi" ? `${w.config.agents.length} asesores` : w.config.agents[0]?.name}</td>
                    <td className="num">{fmtNum(w.impressions)}</td>
                    <td className="num">{fmtNum(w.clicks)}</td>
                    <td className="num">{w.ctr_pct == null ? "—" : `${w.ctr_pct}%`}</td>
                    <td>{w.is_active ? <Badge tone="ok">Activo</Badge> : <Badge tone="neutral">Pausado</Badge>}</td>
                    <td className="nowrap">
                      <button className="link small" onClick={() => setEmbed(w)}>Código</button>{" "}
                      {isAdmin && (
                        <>
                          <button className="link small" onClick={() => { setActionError(null); setDraft({ id: w.id, name: w.name, is_active: w.is_active, link_id: w.link_id, config: w.config }); }}>
                            Editar
                          </button>{" "}
                          <button className="link small danger" onClick={() => remove(w)}>Eliminar</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {draft && cfg && (
        <Modal
          title={draft.id ? "Editar botón" : "Nuevo botón de WhatsApp"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !draft.name || cfg.agents.every((a) => !a.name)} onClick={save}>
                Guardar
              </button>
            </>
          }
        >
          <ErrorBox error={actionError} />
          <div className="grid2" style={{ alignItems: "start" }}>
            <div className="stack" style={{ gap: 8 }}>
              <Field label="Nombre interno">
                <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              </Field>
              <Field label="Tipo">
                <select value={cfg.mode} onChange={(e) => setCfg({ mode: e.target.value as WidgetConfig["mode"] })}>
                  <option value="single">Un solo número (abre WhatsApp directo)</option>
                  <option value="multi">Varios asesores (tarjeta para elegir)</option>
                </select>
              </Field>
              <div className="grid2">
                <Field label="Texto del botón">
                  <input value={cfg.button.text ?? ""} maxLength={40} onChange={(e) => setCfg({ button: { ...cfg.button, text: e.target.value || null } })} />
                </Field>
                <Field label="Color">
                  <input type="color" value={cfg.button.color} onChange={(e) => setCfg({ button: { ...cfg.button, color: e.target.value } })} />
                </Field>
              </div>
              <Field label="Posición">
                <select value={cfg.button.position} onChange={(e) => setCfg({ button: { ...cfg.button, position: e.target.value as "left" | "right" } })}>
                  <option value="right">Abajo a la derecha</option>
                  <option value="left">Abajo a la izquierda</option>
                </select>
              </Field>
              {cfg.mode === "multi" && (
                <>
                  <Field label="Título de la tarjeta">
                    <input value={cfg.title} onChange={(e) => setCfg({ title: e.target.value })} />
                  </Field>
                  <Field label="Saludo">
                    <input value={cfg.greeting ?? ""} onChange={(e) => setCfg({ greeting: e.target.value || null })} />
                  </Field>
                </>
              )}
              <Field label="Mensaje disparador por defecto" hint="Atribuye cada clic (visita, UTMs, gclid/fbclid)">
                <select value={draft.link_id ?? ""} onChange={(e) => setDraft({ ...draft, link_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">Ninguno (wa.me con el texto de cada asesor)</option>
                  {(links.data ?? []).map((l) => (
                    <option key={l.id} value={l.id}>{l.name}</option>
                  ))}
                </select>
              </Field>
            </div>
            <Preview c={cfg} />
          </div>

          <h4 style={{ margin: "12px 0 4px" }}>{cfg.mode === "multi" ? "Asesores" : "Número"}</h4>
          {(cfg.mode === "multi" ? cfg.agents : cfg.agents.slice(0, 1)).map((a, i) => (
            <fieldset key={i} className="card-sub" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 8, marginBottom: 8 }}>
              <legend className="small">{a.name || `Asesor ${i + 1}`}</legend>
              <div className="grid2">
                <Field label="Nombre">
                  <input value={a.name} onChange={(e) => setAgent(i, { name: e.target.value })} />
                </Field>
                <Field label="Cargo / descripción">
                  <input value={a.role ?? ""} onChange={(e) => setAgent(i, { role: e.target.value || null })} />
                </Field>
                <Field label="Número de WhatsApp">
                  <select value={a.channel_id ?? ""} onChange={(e) => setAgent(i, { channel_id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">Principal</option>
                    {waChannels.map((c) => (
                      <option key={c.id} value={c.id}>{c.name} {c.display_phone ?? ""}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Mensaje disparador">
                  <select value={a.link_id ?? ""} onChange={(e) => setAgent(i, { link_id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">El del botón</option>
                    {(links.data ?? []).map((l) => (
                      <option key={l.id} value={l.id}>{l.name}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Texto prellenado (sin mensaje disparador)">
                  <input value={a.prefill ?? ""} onChange={(e) => setAgent(i, { prefill: e.target.value || null })} />
                </Field>
                <Field label="Foto (URL https)">
                  <input value={a.photo_url ?? ""} onChange={(e) => setAgent(i, { photo_url: e.target.value || null })} />
                </Field>
              </div>
              <Toggle checked={!!a.hours} onChange={(v) => setAgent(i, { hours: v ? { days: [0, 1, 2, 3, 4], from: "08:00", to: "18:00" } : null })}
                label="Horario de atención" />
              {a.hours && (
                <div className="inline" style={{ gap: 6, marginTop: 6 }}>
                  {DAY_LABELS.map((d, di) => (
                    <label key={di} className="small" style={{ display: "inline-flex", gap: 2 }}>
                      <input type="checkbox" checked={a.hours!.days.includes(di)}
                        onChange={(e) => setAgent(i, { hours: { ...a.hours!, days: e.target.checked ? [...a.hours!.days, di].sort() : a.hours!.days.filter((x) => x !== di) } })} />
                      {d}
                    </label>
                  ))}
                  <input type="time" aria-label="Desde" value={a.hours.from} onChange={(e) => setAgent(i, { hours: { ...a.hours!, from: e.target.value } })} />
                  <input type="time" aria-label="Hasta" value={a.hours.to} onChange={(e) => setAgent(i, { hours: { ...a.hours!, to: e.target.value } })} />
                </div>
              )}
              {cfg.mode === "multi" && cfg.agents.length > 1 && (
                <button type="button" className="link small danger" onClick={() => setCfg({ agents: cfg.agents.filter((_, j) => j !== i) })}>
                  Quitar asesor
                </button>
              )}
            </fieldset>
          ))}
          {cfg.mode === "multi" && cfg.agents.length < 10 && (
            <button type="button" onClick={() => setCfg({ agents: [...cfg.agents, { ...NEW_AGENT }] })}>+ Asesor</button>
          )}

          <h4 style={{ margin: "12px 0 4px" }}>Dónde aparece</h4>
          <div className="grid2">
            <Field label="Solo en estas páginas" hint="Una por línea; * comodín (ej. /vehiculos/*). Vacío = todas.">
              <textarea rows={2} value={cfg.show_on.paths_include.join("\n")} onChange={(e) => setCfg({ show_on: { ...cfg.show_on, paths_include: lines(e.target.value) } })} />
            </Field>
            <Field label="Nunca en estas páginas">
              <textarea rows={2} value={cfg.show_on.paths_exclude.join("\n")} onChange={(e) => setCfg({ show_on: { ...cfg.show_on, paths_exclude: lines(e.target.value) } })} />
            </Field>
            <Field label="Mostrar después de (segundos)">
              <input type="number" min={0} max={600} value={cfg.show_on.delay_s} onChange={(e) => setCfg({ show_on: { ...cfg.show_on, delay_s: Number(e.target.value) || 0 } })} />
            </Field>
            <Field label="Dominios permitidos" hint="Separados por coma. Vacío = cualquier sitio.">
              <input value={cfg.allowed_domains.join(", ")} onChange={(e) => setCfg({ allowed_domains: lines(e.target.value) })} />
            </Field>
          </div>
          <div className="inline">
            <Toggle checked={cfg.show_on.mobile} onChange={(v) => setCfg({ show_on: { ...cfg.show_on, mobile: v } })} label="Celular" />
            <Toggle checked={cfg.show_on.desktop} onChange={(v) => setCfg({ show_on: { ...cfg.show_on, desktop: v } })} label="Computador" />
            <Toggle checked={draft.is_active} onChange={(v) => setDraft({ ...draft, is_active: v })} label="Activo" />
          </div>
        </Modal>
      )}

      {embed && (
        <Modal title={`Código · ${embed.name}`} onClose={() => setEmbed(null)}>
          <p className="small">
            Pega esta línea antes de <code>&lt;/body&gt;</code> en tu sitio (o en una etiqueta HTML personalizada de Google Tag Manager).
          </p>
          <CodeBlock code={embed.embed} />
        </Modal>
      )}
    </>
  );
}
