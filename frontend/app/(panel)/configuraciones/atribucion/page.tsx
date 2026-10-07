"use client";

import { useState } from "react";
import Link from "next/link";
import { fmtDate, fmtNum, send, type Channel } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { CodeBlock, planError } from "@/components/attribution/common";
import type { TrackingSite, TrackingSiteIn } from "@/lib/attribution-types";

const EMPTY: TrackingSiteIn = { name: "", allowed_domains: [], channel_id: null, wa_prefill: "Hola, quiero más información", is_active: true };

export default function AtribucionPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<TrackingSite[]>("/api/tracking-sites");
  const channels = useApi<{ channels: Channel[] }>("/api/channels");
  const [editing, setEditing] = useState<{ id: number | null; form: TrackingSiteIn; domains: string } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  function edit(site?: TrackingSite) {
    setActionError(null);
    const form = site
      ? { name: site.name, allowed_domains: site.allowed_domains, channel_id: site.channel_id, wa_prefill: site.wa_prefill, is_active: site.is_active }
      : EMPTY;
    setEditing({ id: site?.id ?? null, form, domains: form.allowed_domains.join("\n") });
  }

  async function save() {
    if (!editing) return;
    const body = { ...editing.form, allowed_domains: editing.domains.split(/[\s,]+/).filter(Boolean) };
    const r = await run(() =>
      editing.id ? send<TrackingSite>(`/api/tracking-sites/${editing.id}`, "PUT", body) : send<TrackingSite>("/api/tracking-sites", "POST", body),
    );
    if (r) {
      setEditing(null);
      setOpen(r.id);
      reload();
    }
  }

  async function deactivate(site: TrackingSite) {
    if (!confirm(`¿Desactivar «${site.name}»? El script dejará de registrar visitas.`)) return;
    await run(() => send(`/api/tracking-sites/${site.id}`, "DELETE"));
    reload();
  }

  const set = <K extends keyof TrackingSiteIn>(k: K, v: TrackingSiteIn[K]) =>
    setEditing((e) => (e ? { ...e, form: { ...e.form, [k]: v } } : e));

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Atribución web: une la visita del sitio (fuente, campaña, gclid/fbclid) con la conversación de WhatsApp."
      />
      <ConfigTabs />
      <AdminNotice />

      <Card title="Cómo funciona">
        <ol className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
          <li>Crea un sitio y pega el script en tu web (directo o con Google Tag Manager).</li>
          <li>El script guarda la fuente de la visita (UTM, gclid, fbclid) en una cookie propia y reescribe los enlaces wa.me.</li>
          <li>
            El mensaje prellenado lleva un código <code>(ref: ABC123)</code>; cuando el cliente escribe, la conversación queda
            atribuida a esa visita.
          </li>
          <li>
            Las ventas se envían como conversiones a Google Ads y Meta desde <Link href="/configuraciones/conversiones">Conversiones</Link>.
          </li>
        </ol>
      </Card>

      <Card
        title="Sitios rastreados"
        actions={isAdmin && <button className="primary" onClick={() => edit()}>+ Nuevo sitio</button>}
      >
        <ErrorBox error={planError(error || actionError)} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>Aún no hay sitios. Crea uno para obtener el script de seguimiento.</Empty>
        ) : (
          <div className="stack">
            {data.map((site) => (
              <div key={site.id} className="card" style={{ margin: 0 }}>
                <div className="inline" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
                  <div>
                    <div className="strong">
                      {site.name} {!site.is_active && <Badge>Inactivo</Badge>}
                    </div>
                    <div className="small muted">
                      {site.allowed_domains.length ? site.allowed_domains.join(", ") : "Cualquier dominio"} · creado {fmtDate(site.created_at)}
                    </div>
                  </div>
                  <div className="inline">
                    <span className="small">
                      <strong>{fmtNum(site.sessions_7d)}</strong> visitas · <strong>{fmtNum(site.clicks_7d)}</strong> clics a WA (7 días)
                    </span>
                    <button onClick={() => setOpen(open === site.id ? null : site.id)}>{open === site.id ? "Ocultar código" : "Ver código"}</button>
                    {isAdmin && <button onClick={() => edit(site)}>Editar</button>}
                    {isAdmin && site.is_active && (
                      <button className="danger" disabled={busy} onClick={() => deactivate(site)}>
                        Desactivar
                      </button>
                    )}
                  </div>
                </div>
                {open === site.id && <SiteCode site={site} />}
              </div>
            ))}
          </div>
        )}
      </Card>

      {editing && (
        <Modal
          title={editing.id ? "Editar sitio" : "Nuevo sitio"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button onClick={() => setEditing(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !editing.form.name.trim()} onClick={save}>
                {busy ? "Guardando…" : "Guardar"}
              </button>
            </>
          }
        >
          <div className="form">
            <ErrorBox error={actionError} />
            <Field label="Nombre">
              <input value={editing.form.name} onChange={(e) => set("name", e.target.value)} placeholder="Sitio principal" autoFocus />
            </Field>
            <Field
              label="Dominios permitidos"
              hint="Uno por línea. Se aceptan subdominios (tienda.com cubre www.tienda.com). Vacío = cualquier dominio (no recomendado)."
            >
              <textarea
                rows={3}
                value={editing.domains}
                onChange={(e) => setEditing({ ...editing, domains: e.target.value })}
                placeholder={"tienda.com\nlanding.tienda.com"}
              />
            </Field>
            <Field label="Número de WhatsApp" hint="Al que apunta el botón; por defecto el primer número conectado.">
              <select
                value={editing.form.channel_id ?? ""}
                onChange={(e) => set("channel_id", e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">Predeterminado</option>
                {channels.data?.channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} {c.display_phone ? `(${c.display_phone})` : ""}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Mensaje prellenado" hint="Se añade automáticamente «(ref: CÓDIGO)» al final.">
              <input value={editing.form.wa_prefill} onChange={(e) => set("wa_prefill", e.target.value)} />
            </Field>
            <Toggle checked={editing.form.is_active} onChange={(v) => set("is_active", v)} label="Activo" />
          </div>
        </Modal>
      )}
    </>
  );
}

function SiteCode({ site }: { site: TrackingSite }) {
  return (
    <div className="stack" style={{ marginTop: 12 }}>
      <CodeBlock label="1 · Script (pégalo antes de </head> en todas las páginas)" code={site.snippet} />
      <div className="small muted">
        <strong>Con Google Tag Manager:</strong> crea una etiqueta <em>HTML personalizado</em> con el código de abajo, activador{" "}
        <em>All Pages</em> (o <em>Initialization – All Pages</em>) y publica el contenedor. Si tu sitio usa banner de cookies,
        condiciona la etiqueta al consentimiento de analítica.
      </div>
      <CodeBlock label="GTM · HTML personalizado" code={site.gtm_snippet} />
      <CodeBlock label="2 · Enlace de WhatsApp para botones (opcional)" code={site.wa_link} />
      <div className="small muted">
        El script reescribe automáticamente los enlaces <code>wa.me</code> y <code>api.whatsapp.com</code> de la página. Usa el
        enlace de arriba si el botón se genera fuera de la página (por ejemplo, en un widget o en un correo).
      </div>
    </div>
  );
}
