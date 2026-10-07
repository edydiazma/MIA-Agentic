"use client";

import { useEffect, useState } from "react";
import { api, fmtDateTime, fmtNum, qs, send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { fmtMoney, planError, UploadBadge } from "@/components/attribution/common";
import {
  channelLabel,
  DESTINATION_LABEL,
  TRIGGER_LABEL,
  type ActionOptions,
  type Connection,
  type ConversionAction,
  type ConversionActionIn,
  type ConversionUpload,
} from "@/lib/attribution-types";

const NEW_ACTION: ConversionActionIn = {
  name: "",
  trigger: "typification",
  typification_id: null,
  value: null,
  currency: "COP",
  google_ads: null,
  meta: null,
  is_active: true,
};

export default function ConversionesPage() {
  const isAdmin = useIsAdmin();
  const [notice, setNotice] = useState<string | null>(null);

  // Vuelta del OAuth de Google: ?connected=google_ads | ?error=...
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get("connected") === "google_ads") setNotice("Google Ads conectado. Elige la cuenta de cliente abajo.");
    else if (p.get("error")) setNotice(`No se pudo conectar Google Ads (${p.get("error")}).`);
    if (p.has("connected") || p.has("error")) window.history.replaceState(null, "", window.location.pathname);
  }, []);

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Conversiones: qué cuenta como venta y a qué plataforma de anuncios se reporta (Google Ads, Meta)."
      />
      <ConfigTabs />
      <AdminNotice />
      {notice && <div className="notice">{notice}</div>}
      <Connections isAdmin={isAdmin} />
      <Actions isAdmin={isAdmin} />
      <Uploads isAdmin={isAdmin} />
    </>
  );
}

// ---------- Conexiones ----------
function Connections({ isAdmin }: { isAdmin: boolean }) {
  const { data, error, loading, reload } = useApi<Connection[]>("/api/attribution/connections");
  const [run, busy, actionError] = useAction();
  const google = data?.find((c) => c.provider === "google_ads");
  const meta = data?.find((c) => c.provider === "meta");
  const [customers, setCustomers] = useState<string[] | null>(null);
  const [gForm, setGForm] = useState({ customer_id: "", login_customer_id: "" });
  const [mForm, setMForm] = useState({ access_token: "", waba_id: "", ad_account_id: "" });

  useEffect(() => {
    if (google) setGForm({ customer_id: google.external_account_id ?? "", login_customer_id: google.settings.login_customer_id ?? "" });
    if (meta) setMForm({ access_token: "", waba_id: meta.external_account_id ?? "", ad_account_id: meta.settings.ad_account_id ?? "" });
  }, [google, meta]);

  async function connectGoogle() {
    const r = await run(() => api<{ url: string }>("/api/attribution/connect/google_ads"));
    if (r) window.location.href = r.url;
  }
  async function loadCustomers() {
    const r = await run(() => api<string[]>("/api/attribution/google_ads/customers"));
    if (r) setCustomers(r);
  }
  async function saveGoogle() {
    if (await run(() => send("/api/attribution/connections/google_ads", "PUT", gForm))) reload();
  }
  async function saveMeta() {
    const body = { waba_id: mForm.waba_id, access_token: mForm.access_token || null, ad_account_id: mForm.ad_account_id };
    if (await run(() => send("/api/attribution/connections/meta", "PUT", body))) {
      setMForm((f) => ({ ...f, access_token: "" }));
      reload();
    }
  }
  async function disconnect(provider: string) {
    if (!confirm("¿Desconectar? Las conversiones pendientes quedarán sin enviar hasta reconectar.")) return;
    if (await run(() => send(`/api/attribution/connections/${provider}`, "DELETE"))) reload();
  }

  return (
    <Card title="Conexiones">
      <ErrorBox error={planError(error || actionError)} />
      {loading && !data ? (
        <Loading />
      ) : (
        <div className="grid2">
          {google && (
            <div className="stack">
              <div className="inline" style={{ justifyContent: "space-between" }}>
                <span className="strong">Google Ads</span>
                {google.connected ? <Badge tone="ok">Conectado</Badge> : <Badge>Sin conectar</Badge>}
              </div>
              <p className="small muted" style={{ margin: 0 }}>
                Sube ventas como conversiones de clic (gclid / gbraid / wbraid) con conversiones mejoradas por teléfono.
              </p>
              {!google.configured && (
                <div className="notice small">El servidor no tiene GOOGLE_OAUTH_CLIENT_ID / SECRET configurados.</div>
              )}
              {!google.developer_token && (
                <div className="notice small">Falta GOOGLE_ADS_DEVELOPER_TOKEN en el servidor; las subidas fallarán.</div>
              )}
              {google.last_error && <div className="error small">{google.last_error}</div>}
              {isAdmin && (
                <div className="inline">
                  <button className={google.connected ? "" : "primary"} disabled={busy || !google.configured} onClick={connectGoogle}>
                    {google.connected ? "Reconectar" : "Conectar con Google"}
                  </button>
                  {google.has_token && (
                    <button className="danger" disabled={busy} onClick={() => disconnect("google_ads")}>
                      Desconectar
                    </button>
                  )}
                </div>
              )}
              {google.has_token && (
                <div className="form">
                  <Field
                    label="ID de cliente (cuenta de anuncios)"
                    hint={
                      isAdmin && (
                        <button className="link small" disabled={busy} onClick={loadCustomers}>
                          Ver cuentas accesibles
                        </button>
                      )
                    }
                  >
                    <input
                      value={gForm.customer_id}
                      disabled={!isAdmin}
                      list="gads-customers"
                      onChange={(e) => setGForm({ ...gForm, customer_id: e.target.value })}
                      placeholder="123-456-7890"
                    />
                    <datalist id="gads-customers">
                      {customers?.map((c) => <option key={c} value={c} />)}
                    </datalist>
                  </Field>
                  {customers && <div className="small muted">Cuentas accesibles: {customers.join(", ") || "ninguna"}</div>}
                  <Field label="ID de administrador (MCC)" hint="Solo si accedes a la cuenta a través de un MCC.">
                    <input
                      value={gForm.login_customer_id}
                      disabled={!isAdmin}
                      onChange={(e) => setGForm({ ...gForm, login_customer_id: e.target.value })}
                    />
                  </Field>
                  {isAdmin && (
                    <button className="primary" disabled={busy} onClick={saveGoogle}>
                      Guardar cuenta
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          {meta && (
            <div className="stack">
              <div className="inline" style={{ justifyContent: "space-between" }}>
                <span className="strong">Meta (Conversions API)</span>
                {meta.has_token || meta.server_token ? <Badge tone="ok">Listo</Badge> : <Badge>Sin token</Badge>}
              </div>
              <p className="small muted" style={{ margin: 0 }}>
                Envía eventos de mensajería de WhatsApp con el ctwa_clid de los anuncios Click to WhatsApp al dataset de Meta.
              </p>
              {meta.server_token && !meta.has_token && (
                <div className="small muted">Usando el token de sistema del servidor (META_CAPI_TOKEN).</div>
              )}
              {meta.last_error && <div className="error small">{meta.last_error}</div>}
              <div className="form">
                <Field label="Token de acceso de CAPI" hint={meta.has_token ? "Guardado en Vault. Escribe uno nuevo para reemplazarlo." : "Token de usuario de sistema con permiso sobre el dataset."}>
                  <input
                    type="password"
                    autoComplete="off"
                    value={mForm.access_token}
                    disabled={!isAdmin}
                    onChange={(e) => setMForm({ ...mForm, access_token: e.target.value })}
                    placeholder={meta.has_token ? "••••••••" : ""}
                  />
                </Field>
                <Field label="ID de la cuenta de WhatsApp Business (WABA)">
                  <input value={mForm.waba_id} disabled={!isAdmin} onChange={(e) => setMForm({ ...mForm, waba_id: e.target.value })} />
                </Field>
                <Field label="Cuenta publicitaria de Meta (act_…)"
                  hint="Para traer los nombres de campañas y anuncios y elegir anuncios en los mensajes disparadores. El token necesita el permiso ads_read.">
                  <input value={mForm.ad_account_id} disabled={!isAdmin} placeholder="act_1234567890"
                    onChange={(e) => setMForm({ ...mForm, ad_account_id: e.target.value })} />
                </Field>
                {isAdmin && (
                  <div className="inline">
                    <button className="primary" disabled={busy} onClick={saveMeta}>
                      Guardar
                    </button>
                    {meta.has_token && (
                      <button className="danger" disabled={busy} onClick={() => disconnect("meta")}>
                        Quitar token
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------- Acciones de conversión ----------
function Actions({ isAdmin }: { isAdmin: boolean }) {
  const { data, error, loading, reload } = useApi<ConversionAction[]>("/api/conversion-actions");
  const options = useApi<ActionOptions>("/api/conversion-actions/options");
  const [editing, setEditing] = useState<{ id: number | null; form: ConversionActionIn } | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const [scanMsg, setScanMsg] = useState<string | null>(null);

  function edit(a?: ConversionAction) {
    setActionError(null);
    setEditing(
      a
        ? {
            id: a.id,
            form: {
              name: a.name, trigger: a.trigger, typification_id: a.typification_id, value: a.value, currency: a.currency,
              google_ads: a.google_ads, meta: a.meta, is_active: a.is_active,
            },
          }
        : { id: null, form: NEW_ACTION },
    );
  }

  async function save() {
    if (!editing) return;
    const r = await run(() =>
      editing.id ? send(`/api/conversion-actions/${editing.id}`, "PUT", editing.form) : send("/api/conversion-actions", "POST", editing.form),
    );
    if (r) {
      setEditing(null);
      reload();
    }
  }

  async function deactivate(a: ConversionAction) {
    if (!confirm(`¿Desactivar «${a.name}»? Los envíos ya registrados se conservan.`)) return;
    await run(() => send(`/api/conversion-actions/${a.id}`, "DELETE"));
    reload();
  }

  async function scan() {
    const r = await run(() => send<{ created: number }>("/api/conversion-actions/scan", "POST"));
    if (r) {
      setScanMsg(r.created ? `${fmtNum(r.created)} conversiones nuevas detectadas.` : "No hay conversiones nuevas.");
      reload();
    }
  }

  const f = editing?.form;
  const set = (patch: Partial<ConversionActionIn>) => setEditing((e) => (e ? { ...e, form: { ...e.form, ...patch } } : e));

  return (
    <Card
      title="Acciones de conversión"
      actions={
        isAdmin && (
          <>
            <button disabled={busy} onClick={scan} title="Se revisa automáticamente cada minuto">
              Detectar ahora
            </button>
            <button className="primary" onClick={() => edit()}>
              + Nueva acción
            </button>
          </>
        )
      }
    >
      <ErrorBox error={planError(error || (!editing ? actionError : null))} />
      {scanMsg && <p className="small muted">{scanMsg}</p>}
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Empty>Define qué cuenta como conversión (por ejemplo, cerrar con la tipificación «Venta») y a dónde se envía.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Acción</th>
                <th>Disparador</th>
                <th>Destinos</th>
                <th className="num">Valor</th>
                <th className="num">Últimos 30 días</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((a) => (
                <tr key={a.id} style={a.is_active ? undefined : { opacity: 0.55 }}>
                  <td>
                    <div className="strong">{a.name}</div>
                    {!a.is_active && <Badge>Inactiva</Badge>}
                  </td>
                  <td>
                    {TRIGGER_LABEL[a.trigger] ?? a.trigger}
                    {a.typification && <div className="small muted">«{a.typification}»</div>}
                  </td>
                  <td>
                    <div className="inline" style={{ gap: 4, flexWrap: "wrap" }}>
                      {a.google_ads && <Badge tone="info">Google Ads · {a.google_ads.conversion_action_id}</Badge>}
                      {a.meta && <Badge tone="info">Meta · {a.meta.event_name}</Badge>}
                    </div>
                  </td>
                  <td className="num">{fmtMoney(a.value, a.currency)}</td>
                  <td className="num">{fmtNum(a.events_30d)}</td>
                  <td className="right nowrap">
                    {isAdmin && <button onClick={() => edit(a)}>Editar</button>}{" "}
                    {isAdmin && a.is_active && (
                      <button className="danger" disabled={busy} onClick={() => deactivate(a)}>
                        Desactivar
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && f && (
        <Modal
          wide
          title={editing.id ? "Editar acción de conversión" : "Nueva acción de conversión"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button onClick={() => setEditing(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !f.name.trim()} onClick={save}>
                {busy ? "Guardando…" : "Guardar"}
              </button>
            </>
          }
        >
          <div className="form">
            <ErrorBox error={actionError} />
            <div className="grid2">
              <Field label="Nombre">
                <input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Venta WhatsApp" autoFocus />
              </Field>
              <Field label="Cuándo cuenta">
                <select value={f.trigger} onChange={(e) => set({ trigger: e.target.value as ConversionActionIn["trigger"] })}>
                  {(options.data?.triggers ?? (Object.keys(TRIGGER_LABEL) as ConversionActionIn["trigger"][])).map((t) => (
                    <option key={t} value={t}>
                      {TRIGGER_LABEL[t] ?? t}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {f.trigger === "typification" && (
              <Field label="Tipificación" hint="La conversión se registra cuando un asesor cierra la conversación con esta tipificación.">
                <select value={f.typification_id ?? ""} onChange={(e) => set({ typification_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">Elige…</option>
                  {options.data?.typifications.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <div className="grid2">
              <Field label="Valor" hint={f.trigger === "deal_won" ? "Vacío = usar el valor del negocio." : "Vacío = sin valor."}>
                <input
                  type="number"
                  min={0}
                  value={f.value ?? ""}
                  onChange={(e) => set({ value: e.target.value === "" ? null : Number(e.target.value) })}
                />
              </Field>
              <Field label="Moneda">
                <input value={f.currency} maxLength={3} onChange={(e) => set({ currency: e.target.value.toUpperCase() })} />
              </Field>
            </div>

            <Toggle
              checked={!!f.google_ads}
              onChange={(v) => set({ google_ads: v ? { customer_id: "", conversion_action_id: "" } : null })}
              label="Enviar a Google Ads"
            />
            {f.google_ads && (
              <div className="grid2">
                <Field label="ID de cliente" hint="La cuenta de anuncios donde está la acción.">
                  <input
                    value={f.google_ads.customer_id}
                    onChange={(e) => set({ google_ads: { ...f.google_ads!, customer_id: e.target.value } })}
                    placeholder="123-456-7890"
                  />
                </Field>
                <Field label="ID de la acción de conversión" hint="Google Ads › Objetivos › Conversiones (tipo «Importar · clics»).">
                  <input
                    value={f.google_ads.conversion_action_id}
                    onChange={(e) => set({ google_ads: { ...f.google_ads!, conversion_action_id: e.target.value } })}
                  />
                </Field>
              </div>
            )}

            <Toggle
              checked={!!f.meta}
              onChange={(v) => set({ meta: v ? { dataset_id: "", event_name: "Purchase", whatsapp_business_account_id: null } : null })}
              label="Enviar a Meta (Conversions API)"
            />
            {f.meta && (
              <div className="grid2">
                <Field label="ID del dataset (píxel)">
                  <input value={f.meta.dataset_id} onChange={(e) => set({ meta: { ...f.meta!, dataset_id: e.target.value } })} />
                </Field>
                <Field label="Evento">
                  <select value={f.meta.event_name} onChange={(e) => set({ meta: { ...f.meta!, event_name: e.target.value } })}>
                    {(options.data?.meta_events ?? ["Purchase", "Lead"]).map((ev) => (
                      <option key={ev}>{ev}</option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
            <p className="small muted" style={{ margin: 0 }}>
              Solo se envía a Google si la conversación tiene gclid/gbraid/wbraid, y a Meta si llegó por un anuncio Click to WhatsApp
              (ctwa_clid); en otro caso el envío queda «Omitido».
            </p>
            <Toggle checked={f.is_active} onChange={(v) => set({ is_active: v })} label="Activa" />
          </div>
        </Modal>
      )}
    </Card>
  );
}

// ---------- Log de envíos ----------
function Uploads({ isAdmin }: { isAdmin: boolean }) {
  const [status, setStatus] = useState("");
  const [destination, setDestination] = useState("");
  const { data, error, loading, reload } = useApi<ConversionUpload[]>(
    `/api/conversion-uploads${qs({ status: status || null, destination: destination || null, limit: 200 })}`,
  );
  const [run, busy, actionError] = useAction();

  async function retry(u: ConversionUpload) {
    await run(() => send(`/api/conversion-uploads/${u.id}/retry`, "POST"));
    reload();
  }

  return (
    <Card
      title="Envíos a plataformas"
      actions={
        <div className="inline">
          <select value={destination} onChange={(e) => setDestination(e.target.value)} aria-label="Destino">
            <option value="">Todos los destinos</option>
            <option value="google_ads">Google Ads</option>
            <option value="meta_capi">Meta CAPI</option>
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Estado">
            <option value="">Todos los estados</option>
            <option value="pending">Pendientes</option>
            <option value="sent">Enviadas</option>
            <option value="failed">Fallidas</option>
            <option value="skipped">Omitidas</option>
          </select>
          <button onClick={reload} disabled={loading}>
            Actualizar
          </button>
        </div>
      }
    >
      <ErrorBox error={planError(error || actionError)} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Empty>Sin envíos todavía.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Contacto</th>
                <th>Acción</th>
                <th>Canal</th>
                <th>Destino</th>
                <th>Estado</th>
                <th className="num">Valor</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((u) => (
                <tr key={u.id}>
                  <td className="nowrap">{fmtDateTime(u.occurred_at)}</td>
                  <td>
                    {u.conversation_id ? <a href={`/conversaciones?id=${u.conversation_id}`}>{u.contact}</a> : u.contact}
                  </td>
                  <td>{u.action}</td>
                  <td>{channelLabel(u.channel)}</td>
                  <td>{DESTINATION_LABEL[u.destination] ?? u.destination}</td>
                  <td>
                    <UploadBadge status={u.status} />
                    {u.status === "pending" && u.attempts > 0 && (
                      <div className="small muted">
                        Intento {u.attempts} · próximo {fmtDateTime(u.next_attempt_at)}
                      </div>
                    )}
                    {u.status === "sent" && u.sent_at && <div className="small muted">{fmtDateTime(u.sent_at)}</div>}
                    {u.error && (
                      <div className="small muted" title={u.error} style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {u.error}
                      </div>
                    )}
                  </td>
                  <td className="num">{fmtMoney(u.value, u.currency)}</td>
                  <td className="right">
                    {isAdmin && (u.status === "failed" || u.status === "pending") && (
                      <button className="small" disabled={busy} onClick={() => retry(u)}>
                        Reintentar
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
