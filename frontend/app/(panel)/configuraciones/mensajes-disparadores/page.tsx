"use client";

import { useState } from "react";
import Link from "next/link";
import { api, fmtNum, send, type Channel, type Group } from "@/lib/api";
import type { Flow } from "@/lib/flow-types";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { CodeBlock, fmtMoney, planError } from "@/components/attribution/common";
import {
  PLATFORM_LABEL,
  type GoogleCampaign,
  type LinkPlatform,
  type LinkTestMatch,
  type MetaAd,
  type WaLink,
  type WaLinkIn,
} from "@/lib/attribution-types";

const EMPTY: WaLinkIn = {
  name: "",
  trigger_text: "",
  platform: "web",
  slug: null,
  channel_id: null,
  append_ref: true,
  utm_source: null,
  utm_medium: null,
  utm_campaign: null,
  utm_content: null,
  utm_term: null,
  meta_ad_ids: [],
  google_campaign_ids: [],
  tags: [],
  group_id: null,
  flow_id: null,
  is_active: true,
};

/** Dónde se usa cada formato del enlace, según la plataforma. */
const HOW_TO: Record<LinkPlatform, string> = {
  meta_ads:
    "En el anuncio Click to WhatsApp, pega el mensaje disparador como «mensaje prellenado» y elige abajo los anuncios que lo usan. Meta no admite redirecciones: la conversación se atribuye por el ID del anuncio o por el texto.",
  google_ads:
    "Usa el enlace corto como URL final del anuncio (o de la extensión de mensaje), o tu landing con el sufijo de URL final. Con el etiquetado automático Google agrega el gclid y se sube la conversión offline.",
  web: "Usa el enlace corto en los botones de WhatsApp de tu sitio. Si el sitio tiene el script de Atribución web, la visita se reutiliza.",
  social: "Pon el enlace corto en la biografía, publicaciones o historias.",
  email: "Usa el enlace corto en el botón del correo.",
  qr: "Imprime un QR con el enlace corto (en volantes, vitrinas, vehículos).",
  sms: "Envía el enlace corto en el SMS.",
  other: "Usa el enlace corto donde publiques la campaña.",
};

const lines = (v: string) => v.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);

function toIn(link: WaLink): WaLinkIn {
  return {
    name: link.name,
    trigger_text: link.trigger_text,
    platform: link.platform,
    slug: link.slug,
    channel_id: link.channel_id,
    append_ref: link.append_ref,
    utm_source: link.utm_source,
    utm_medium: link.utm_medium,
    utm_campaign: link.utm_campaign,
    utm_content: link.utm_content,
    utm_term: link.utm_term,
    meta_ad_ids: link.meta_ad_ids,
    google_campaign_ids: link.google_campaign_ids,
    tags: link.tags,
    group_id: link.group_id,
    flow_id: link.flow_id,
    is_active: link.is_active,
  };
}

type Editing = { id: number | null; form: WaLinkIn; adIds: string; campaignIds: string; tags: string };

export default function MensajesDisparadoresPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload, setData } = useApi<WaLink[]>("/api/wa-links");
  const channels = useApi<{ channels: Channel[] }>("/api/channels");
  const groups = useApi<Group[]>("/api/groups");
  const flows = useApi<Flow[]>("/api/flows");
  const [editing, setEditing] = useState<Editing | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  function edit(link?: WaLink) {
    setActionError(null);
    const form = link ? toIn(link) : EMPTY;
    setEditing({
      id: link?.id ?? null,
      form,
      adIds: form.meta_ad_ids.join("\n"),
      campaignIds: form.google_campaign_ids.join("\n"),
      tags: form.tags.join(", "),
    });
  }

  async function save() {
    if (!editing) return;
    const body: WaLinkIn = {
      ...editing.form,
      slug: editing.form.slug?.trim() || null,
      meta_ad_ids: lines(editing.adIds),
      google_campaign_ids: lines(editing.campaignIds),
      tags: lines(editing.tags),
    };
    const r = await run(() =>
      editing.id ? send<WaLink>(`/api/wa-links/${editing.id}`, "PUT", body) : send<WaLink>("/api/wa-links", "POST", body),
    );
    if (r) {
      setEditing(null);
      setOpen(r.id);
      reload();
    }
  }

  async function toggle(link: WaLink) {
    const r = await run(() => send<WaLink>(`/api/wa-links/${link.id}`, "PUT", { ...toIn(link), is_active: !link.is_active }));
    if (r) setData((list) => list?.map((l) => (l.id === r.id ? r : l)) ?? list);
  }

  async function remove(link: WaLink) {
    if (!confirm(`¿Eliminar «${link.name}»? Los enlaces ya publicados dejarán de funcionar; las conversaciones atribuidas conservan sus UTMs.`)) return;
    await run(() => send(`/api/wa-links/${link.id}`, "DELETE"));
    reload();
  }

  const set = <K extends keyof WaLinkIn>(k: K, v: WaLinkIn[K]) => setEditing((e) => (e ? { ...e, form: { ...e.form, [k]: v } } : e));

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Mensajes disparadores: el texto con el que llega el cliente identifica la campaña, aplica etiquetas, grupo y flujo, y alinea la atribución."
      />
      <ConfigTabs />
      <AdminNotice />

      <Card title="Cómo funciona">
        <ol className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
          <li>
            Crea un mensaje disparador por campaña o anuncio (por ejemplo «Hola, quiero la promo de la CX-5») con sus UTMs.
          </li>
          <li>
            Publícalo con el <strong>enlace corto</strong> (captura gclid, fbclid y UTMs y agrega un código «ref») o con el{" "}
            <strong>enlace directo wa.me</strong> en anuncios Click to WhatsApp.
          </li>
          <li>
            Cuando el cliente escribe se reconoce por el anuncio, el código o el texto (aunque borre el código) y la conversación
            queda atribuida. Si vuelve por otro anuncio se registra un toque nuevo.
          </li>
          <li>
            El flujo elegido se inicia (o cualquiera con el disparador «Cuando llega por un mensaje disparador»), y las ventas se
            suben como conversiones desde <Link href="/configuraciones/conversiones">Conversiones</Link>. Rendimiento en{" "}
            <Link href="/reportes/mensajes-disparadores">Reportes › Mensajes disparadores</Link>.
          </li>
        </ol>
      </Card>

      <TestBox />

      <Card title="Mensajes disparadores" actions={isAdmin && <button className="primary" onClick={() => edit()}>+ Nuevo mensaje</button>}>
        <ErrorBox error={planError(error || (editing ? null : actionError))} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>Aún no hay mensajes disparadores. Crea uno por cada campaña o anuncio.</Empty>
        ) : (
          <div className="stack">
            {data.map((link) => (
              <div key={link.id} className="card" style={{ margin: 0 }}>
                <div className="inline" style={{ justifyContent: "space-between" }}>
                  <div style={{ minWidth: 0 }}>
                    <div className="strong">
                      {link.name} <Badge tone="neutral">{PLATFORM_LABEL[link.platform] ?? link.platform}</Badge>{" "}
                      {!link.is_active && <Badge tone="warn">Pausado</Badge>}
                    </div>
                    <div className="small muted">«{link.trigger_text}»</div>
                    <div className="small muted">
                      {[link.utm_source, link.utm_medium, link.utm_campaign].filter(Boolean).join(" / ") || "Sin UTMs"}
                      {link.tags.length > 0 && <> · etiquetas: {link.tags.join(", ")}</>}
                      {link.flow_id && (
                        <>
                          {" "}· flujo:{" "}
                          <Link href={`/automatizaciones/flujos/${link.flow_id}`}>
                            {flows.data?.find((f) => f.id === link.flow_id)?.name ?? `#${link.flow_id}`}
                          </Link>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="inline">
                    <span className="small">
                      <strong>{fmtNum(link.stats.clicks)}</strong> clics · <strong>{fmtNum(link.stats.conversations)}</strong> conversaciones ·{" "}
                      <strong>{fmtNum(link.stats.conversions)}</strong> conversiones
                      {link.stats.conversion_value > 0 && <> ({fmtMoney(link.stats.conversion_value)})</>} <span className="muted">(30 días)</span>
                    </span>
                    <button onClick={() => setOpen(open === link.id ? null : link.id)}>{open === link.id ? "Ocultar enlaces" : "Ver enlaces"}</button>
                    {isAdmin && <Toggle checked={link.is_active} onChange={() => toggle(link)} label="Activo" />}
                    {isAdmin && <button onClick={() => edit(link)}>Editar</button>}
                    {isAdmin && (
                      <button className="danger" disabled={busy} onClick={() => remove(link)}>
                        Eliminar
                      </button>
                    )}
                  </div>
                </div>
                {open === link.id && <LinkCodes link={link} />}
              </div>
            ))}
          </div>
        )}
      </Card>

      {editing && (
        <Modal
          wide
          title={editing.id ? "Editar mensaje disparador" : "Nuevo mensaje disparador"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button onClick={() => setEditing(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !editing.form.name.trim() || !editing.form.trigger_text.trim()} onClick={save}>
                {busy ? "Guardando…" : "Guardar"}
              </button>
            </>
          }
        >
          <div className="form">
            <ErrorBox error={actionError} />
            <div className="grid2">
              <Field label="Nombre">
                <input value={editing.form.name} onChange={(e) => set("name", e.target.value)} placeholder="Promo CX-5 · Meta" autoFocus />
              </Field>
              <Field label="Plataforma">
                <select value={editing.form.platform} onChange={(e) => set("platform", e.target.value as LinkPlatform)}>
                  {(Object.keys(PLATFORM_LABEL) as LinkPlatform[]).map((p) => (
                    <option key={p} value={p}>
                      {PLATFORM_LABEL[p]}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <p className="small muted" style={{ margin: 0 }}>
              {HOW_TO[editing.form.platform]}
            </p>
            <Field
              label="Mensaje disparador"
              hint="Lo que el cliente envía al abrir WhatsApp. Mínimo 8 letras y distinto de los demás mensajes activos; no incluyas «ref:», se agrega solo."
            >
              <textarea rows={2} value={editing.form.trigger_text} onChange={(e) => set("trigger_text", e.target.value)} placeholder="Hola, quiero la promo de la CX-5" />
            </Field>
            <Toggle
              checked={editing.form.append_ref}
              onChange={(v) => set("append_ref", v)}
              label="Agregar el código «(ref: …)» en el enlace corto (une el clic con su gclid/fbclid)"
            />

            <div className="grid2">
              <Field label="utm_source" hint="Si lo dejas vacío se sugiere según la plataforma.">
                <input value={editing.form.utm_source ?? ""} onChange={(e) => set("utm_source", e.target.value || null)} />
              </Field>
              <Field label="utm_medium">
                <input value={editing.form.utm_medium ?? ""} onChange={(e) => set("utm_medium", e.target.value || null)} />
              </Field>
              <Field label="utm_campaign" hint="Vacío = derivado del nombre.">
                <input value={editing.form.utm_campaign ?? ""} onChange={(e) => set("utm_campaign", e.target.value || null)} />
              </Field>
              <Field label="utm_content">
                <input value={editing.form.utm_content ?? ""} onChange={(e) => set("utm_content", e.target.value || null)} />
              </Field>
              <Field label="utm_term">
                <input value={editing.form.utm_term ?? ""} onChange={(e) => set("utm_term", e.target.value || null)} />
              </Field>
              <Field label="Slug del enlace corto" hint="Vacío = automático. Cambiarlo rompe los enlaces ya publicados.">
                <input value={editing.form.slug ?? ""} onChange={(e) => set("slug", e.target.value || null)} placeholder="promo-cx5" />
              </Field>
            </div>

            {editing.form.platform === "meta_ads" && (
              <MetaAdsPicker value={editing.adIds} onChange={(v) => setEditing({ ...editing, adIds: v })} />
            )}
            {editing.form.platform === "google_ads" && (
              <GoogleCampaignPicker value={editing.campaignIds} onChange={(v) => setEditing({ ...editing, campaignIds: v })} />
            )}

            <h3 style={{ margin: "4px 0 0" }}>Al llegar</h3>
            <div className="grid2">
              <Field label="Etiquetas" hint="Separadas por coma.">
                <input value={editing.tags} onChange={(e) => setEditing({ ...editing, tags: e.target.value })} placeholder="promo cx5, meta" />
              </Field>
              <Field label="Grupo">
                <select value={editing.form.group_id ?? ""} onChange={(e) => set("group_id", e.target.value ? Number(e.target.value) : null)}>
                  <option value="">Sin cambio</option>
                  {groups.data?.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Flujo" hint="Se inicia su script «Cuando llega por un mensaje disparador» o, si no tiene, el primero.">
                <select value={editing.form.flow_id ?? ""} onChange={(e) => set("flow_id", e.target.value ? Number(e.target.value) : null)}>
                  <option value="">Ninguno (responde el bot)</option>
                  {flows.data?.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.name} {f.status !== "active" ? `(${f.status})` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Número de WhatsApp">
                <select value={editing.form.channel_id ?? ""} onChange={(e) => set("channel_id", e.target.value ? Number(e.target.value) : null)}>
                  <option value="">Predeterminado</option>
                  {channels.data?.channels.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} {c.display_phone ? `(${c.display_phone})` : ""}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Toggle checked={editing.form.is_active} onChange={(v) => set("is_active", v)} label="Activo" />
          </div>
        </Modal>
      )}
    </>
  );
}

function LinkCodes({ link }: { link: WaLink }) {
  return (
    <div className="stack" style={{ marginTop: 12 }}>
      <CodeBlock label="Enlace corto con tracking (web, redes, correo, QR, Google Ads)" code={link.short_url} />
      <CodeBlock label="Enlace directo wa.me (anuncios Click to WhatsApp, sin redirección)" code={link.direct_url} />
      {link.google_final_url_suffix && (
        <CodeBlock label="Google Ads · Sufijo de URL final (si el anuncio va a tu landing)" code={link.google_final_url_suffix} />
      )}
      <div className="small muted">
        Para un QR, genera el código con el enlace corto en tu herramienta de diseño: así cada escaneo queda registrado como clic.
      </div>
    </div>
  );
}

function TestBox() {
  const [text, setText] = useState("");
  const [result, setResult] = useState<LinkTestMatch | null>(null);
  const [run, busy, error] = useAction();
  async function test() {
    const r = await run(() => send<LinkTestMatch>("/api/wa-links/test-match", "POST", { text }));
    if (r) setResult(r);
  }
  return (
    <Card title="Probar un mensaje">
      <div className="inline">
        <input
          style={{ flex: 1, minWidth: 220 }}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setResult(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && text.trim() && test()}
          placeholder="Escribe el mensaje como lo enviaría un cliente"
        />
        <button disabled={busy || !text.trim()} onClick={test}>
          Probar
        </button>
      </div>
      <ErrorBox error={planError(error)} />
      {result && (
        <p className="small" style={{ marginBottom: 0 }}>
          {result.link ? (
            <>
              ✅ Se reconoce como <strong>{result.link.name}</strong> <span className="muted">({result.link.slug})</span>
            </>
          ) : (
            <>❌ No coincide con ningún mensaje disparador activo.</>
          )}
          {result.ref_code && (
            <>
              {" "}· Código de visita <code>{result.ref_code}</code> (tiene prioridad sobre el texto)
            </>
          )}
        </p>
      )}
    </Card>
  );
}

/** Lista de IDs (uno por línea) con buscador de anuncios de Meta si la cuenta está conectada. */
function MetaAdsPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [q, setQ] = useState("");
  const [ads, setAds] = useState<MetaAd[] | null>(null);
  const [run, busy, error] = useAction();
  const ids = lines(value);
  async function search() {
    const r = await run(() => api<MetaAd[]>(`/api/wa-links/meta-ads?q=${encodeURIComponent(q)}`));
    if (r) setAds(r);
  }
  const add = (id: string) => !ids.includes(id) && onChange([...ids, id].join("\n"));
  return (
    <Field label="Anuncios de Meta que usan este mensaje" hint="Un ID por línea. Con la cuenta de Meta conectada puedes buscarlos.">
      <textarea rows={2} value={value} onChange={(e) => onChange(e.target.value)} placeholder="120208000000000001" />
      <div className="inline" style={{ marginTop: 6 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar anuncio por nombre" onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), search())} />
        <button type="button" disabled={busy} onClick={search}>
          {busy ? "Buscando…" : "Buscar"}
        </button>
      </div>
      <PickerError error={error} what="Meta" />
      {ads && (
        <div className="stack" style={{ gap: 4, marginTop: 6, maxHeight: 180, overflowY: "auto" }}>
          {ads.length === 0 && <span className="small muted">Sin resultados</span>}
          {ads.map((ad) => (
            <button type="button" key={ad.id} className="link small" style={{ textAlign: "left" }} disabled={ids.includes(ad.id)} onClick={() => add(ad.id)}>
              {ids.includes(ad.id) ? "✓ " : "+ "}
              {ad.name} <span className="muted">· {[ad.campaign_name, ad.adset_name].filter(Boolean).join(" › ")} · {ad.status}</span>
            </button>
          ))}
        </div>
      )}
    </Field>
  );
}

function GoogleCampaignPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [list, setList] = useState<GoogleCampaign[] | null>(null);
  const [run, busy, error] = useAction();
  const ids = lines(value);
  async function load() {
    const r = await run(() => api<GoogleCampaign[]>("/api/wa-links/google-campaigns"));
    if (r) setList(r);
  }
  const add = (id: string) => !ids.includes(id) && onChange([...ids, id].join("\n"));
  return (
    <Field label="Campañas de Google Ads" hint="Opcional: un ID por línea. Sirve para agrupar el reporte por campaña.">
      <textarea rows={2} value={value} onChange={(e) => onChange(e.target.value)} placeholder="21000000001" />
      <div className="inline" style={{ marginTop: 6 }}>
        <button type="button" disabled={busy} onClick={load}>
          {busy ? "Cargando…" : "Cargar campañas"}
        </button>
      </div>
      <PickerError error={error} what="Google Ads" />
      {list && (
        <div className="stack" style={{ gap: 4, marginTop: 6, maxHeight: 180, overflowY: "auto" }}>
          {list.length === 0 && <span className="small muted">Sin campañas</span>}
          {list.map((c) => (
            <button type="button" key={c.id} className="link small" style={{ textAlign: "left" }} disabled={ids.includes(c.id)} onClick={() => add(c.id)}>
              {ids.includes(c.id) ? "✓ " : "+ "}
              {c.name} <span className="muted">· {c.id} · {c.status}</span>
            </button>
          ))}
        </div>
      )}
    </Field>
  );
}

function PickerError({ error, what }: { error: string | null; what: string }) {
  if (!error) return null;
  const notConnected = /409|conect|connect/i.test(error);
  return (
    <div className="small muted" style={{ marginTop: 6 }}>
      {notConnected ? (
        <>
          {error}. Conecta {what} en <Link href="/configuraciones/conversiones">Conversiones</Link> o escribe los IDs a mano.
        </>
      ) : (
        <span style={{ color: "var(--bad)" }}>{error}</span>
      )}
    </div>
  );
}
