"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, fmtDateTime, qs, send, timeAgo } from "@/lib/api";
import type { CRMProvider, CRMStatus, OutboxRow, PushContacts } from "@/lib/crm-types";
import { ENTITY_LABEL, OUTBOX_STATUS_LABEL } from "@/lib/crm-types";
import { Badge, Card, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import MappingsEditor from "@/components/crm/MappingsEditor";
import HubConnections from "@/components/hub/HubConnections";

const DESCRIPTION: Record<CRMProvider, string> = {
  hubspot:
    "Crea y actualiza contactos y negocios en HubSpot, agrega una nota con el resumen al cerrar cada conversación y trae de vuelta los cambios hechos en HubSpot.",
  salesforce:
    "Sincroniza clientes como Contactos o Leads y los negocios como Oportunidades. Al cerrar una conversación registra una Tarea con el resumen.",
  zoho: "Crea y actualiza Contactos y Negocios (Deals) en Zoho CRM, agrega una nota con el resumen al cerrar la conversación y trae los cambios.",
  odoo: "Sincroniza clientes como Contactos (res.partner) y negocios como Oportunidades del CRM de Odoo, con notas en el chatter.",
};
const PROVIDER_NAME: Record<string, string> = {
  hubspot: "HubSpot", salesforce: "Salesforce", zoho: "Zoho CRM", odoo: "Odoo", shopify: "Shopify",
  google_calendar: "Google Calendar", microsoft_calendar: "Outlook",
};

export default function IntegracionesPage() {
  return (
    <Suspense fallback={null}>
      <Integraciones />
    </Suspense>
  );
}

function Integraciones() {
  const params = useSearchParams();
  const router = useRouter();
  const status = useApi<CRMStatus[]>("/api/integrations/crm");
  const [notice, setNotice] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  // Regreso del OAuth: ?provider=hubspot&connected=1 | &error=...
  useEffect(() => {
    const provider = params.get("provider");
    if (!provider) return;
    const name = PROVIDER_NAME[provider] ?? provider;
    if (params.get("connected")) setNotice({ tone: "ok", text: `${name} quedó conectado. La primera sincronización empieza en unos segundos.` });
    else if (params.get("error")) setNotice({ tone: "bad", text: `${name}: ${params.get("error")}` });
    router.replace("/configuraciones/integraciones");
  }, [params, router]);

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Integraciones: CRM, tiendas en línea y calendarios para sincronizar clientes, negocios, pedidos y citas." />
      <ConfigTabs />
      <AdminNotice />
      {notice && (
        <div
          className="error-box"
          style={notice.tone === "ok" ? { background: "var(--ok-soft)", color: "var(--ok)" } : undefined}
          role="status"
        >
          {notice.text}{" "}
          <button className="link small" onClick={() => setNotice(null)}>
            Cerrar
          </button>
        </div>
      )}
      <ErrorBox error={status.error} />
      {!status.data ? (
        <Loading />
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          {status.data.map((s) => (
            <ProviderCard
              key={s.provider}
              status={s}
              onChange={(next) => status.setData((all) => all && all.map((x) => (x.provider === next.provider ? next : x)))}
              reload={status.reload}
            />
          ))}
        </div>
      )}
      <div style={{ marginTop: 24 }}>
        <HubConnections />
      </div>
    </>
  );
}

function ProviderCard({ status: s, onChange, reload }: { status: CRMStatus; onChange: (s: CRMStatus) => void; reload: () => void }) {
  const isAdmin = useIsAdmin();
  const [run, busy, error] = useAction();
  const [tokenModal, setTokenModal] = useState(false);
  const [odooModal, setOdooModal] = useState(false);
  const [panel, setPanel] = useState<"mappings" | "outbox" | null>(null);
  const [mappingsKey, setMappingsKey] = useState(0);
  const base = `/api/integrations/${s.provider}`;

  async function connect() {
    const r = await run(() => api<{ url: string }>(`${base}/connect`));
    if (r) window.location.href = r.url;
  }
  async function disconnect() {
    if (!confirm(`¿Desconectar ${s.label}? Se borran los tokens y los vínculos; los datos en el CRM no se tocan.`)) return;
    if (await run(() => send(`${base}`, "DELETE"))) reload();
  }
  async function syncNow() {
    const r = await run(() => send<{ status: CRMStatus }>(`${base}/sync`, "POST"));
    if (r) onChange(r.status);
  }
  async function saveSettings(patch: Record<string, unknown>) {
    const r = await run(() => send<CRMStatus>(`${base}/settings`, "PUT", patch));
    if (r) onChange(r);
  }

  const tone = !s.connected ? "neutral" : s.status === "connected" ? "ok" : "bad";
  const label = !s.connected ? "Sin conectar" : s.status === "connected" ? "Conectado" : "Error de conexión";

  return (
    <Card
      title={
        <span className="inline">
          {s.label} <Badge tone={tone}>{label}</Badge>
        </span>
      }
      actions={
        isAdmin &&
        (s.connected ? (
          <>
            <button disabled={busy || s.status !== "connected"} onClick={syncNow}>
              {busy ? "Sincronizando…" : "Sincronizar ahora"}
            </button>
            {s.status === "error" && s.via_oauth && (
              <button className="primary" disabled={busy || !s.configured} onClick={connect}>
                Reconectar
              </button>
            )}
            <button className="danger" disabled={busy} onClick={disconnect}>
              Desconectar
            </button>
          </>
        ) : (
          <>
            {s.provider === "hubspot" && <button onClick={() => setTokenModal(true)}>Usar token de app privada</button>}
            {s.provider === "odoo" ? (
              <button className="primary" onClick={() => setOdooModal(true)}>
                Conectar con clave de API
              </button>
            ) : (
              <button className="primary" disabled={busy || !s.configured} onClick={connect}>
                Conectar con {s.label}
              </button>
            )}
          </>
        ))
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        {DESCRIPTION[s.provider]}
      </p>
      {!s.connected && !s.configured && (
        <p className="small" style={{ color: "var(--warn)" }}>
          El servidor no tiene configurada la app de {s.label} ({s.provider.toUpperCase()}_CLIENT_ID y _CLIENT_SECRET).
          {s.provider === "hubspot" && " Mientras tanto puedes conectar con un token de app privada."}
        </p>
      )}

      {s.connected && (
        <div className="stack" style={{ gap: 12 }}>
          <div className="grid4">
            <div>
              <div className="small muted">Cuenta</div>
              <div className="small">
                {s.provider === "hubspot" ? `Portal ${s.external_account_id ?? "—"}` : s.instance_url ?? s.external_account_id ?? "—"}
                {!s.via_oauth && <span className="muted"> (token privado)</span>}
              </div>
            </div>
            <div>
              <div className="small muted">Última sincronización</div>
              <div className="small" title={fmtDateTime(s.last_sync_at)}>
                {timeAgo(s.last_sync_at)}
              </div>
            </div>
            <div>
              <div className="small muted">Último cambio traído del CRM</div>
              <div className="small">{timeAgo(s.settings?.last_pull_at)}</div>
            </div>
            <div>
              <div className="small muted">Cola</div>
              <div className="small">
                {s.outbox?.pending ?? 0} pendientes · {s.outbox?.sent ?? 0} enviados ·{" "}
                <span style={{ color: s.outbox?.failed ? "var(--bad)" : undefined }}>{s.outbox?.failed ?? 0} fallidos</span>
              </div>
            </div>
          </div>
          {s.last_error && <div className="error-box">{s.last_error}</div>}

          <div className="grid2">
            <div className="stack" style={{ gap: 8 }}>
              <Toggle
                checked={!!s.sync_enabled}
                onChange={(v) => isAdmin && saveSettings({ sync_enabled: v })}
                label="Sincronización automática"
              />
              <Toggle
                checked={!!s.settings?.push_notes}
                onChange={(v) => isAdmin && saveSettings({ push_notes: v })}
                label="Registrar nota al cerrar la conversación"
              />
            </div>
            <div className="stack" style={{ gap: 8 }}>
              <Field label="Clientes que se envían">
                <select
                  disabled={!isAdmin || busy}
                  value={s.settings?.push_contacts ?? "all"}
                  onChange={(e) => saveSettings({ push_contacts: e.target.value as PushContacts })}
                >
                  <option value="all">Todos los clientes</option>
                  <option value="with_deal">Solo los que tienen un negocio</option>
                  <option value="none">Ninguno (solo negocios y notas)</option>
                </select>
              </Field>
              {s.provider === "salesforce" && (
                <Field label="Crear clientes como" hint="Cambiarlo no mueve los registros ya vinculados.">
                  <select
                    disabled={!isAdmin || busy}
                    value={s.settings?.contact_object ?? "Contact"}
                    onChange={(e) => saveSettings({ contact_object: e.target.value })}
                  >
                    <option value="Contact">Contactos</option>
                    <option value="Lead">Leads (prospectos)</option>
                  </select>
                </Field>
              )}
            </div>
          </div>

          {isAdmin && (s.provider === "hubspot" || s.provider === "salesforce") && (
            <AttributionSync
              provider={s.provider}
              onMapped={() => {
                setMappingsKey((k) => k + 1);
                setPanel("mappings");
              }}
            />
          )}

          <div className="inline">
            <button className="link" onClick={() => setPanel(panel === "mappings" ? null : "mappings")}>
              {panel === "mappings" ? "Ocultar mapeo de campos" : "Mapeo de campos"}
            </button>
            <button className="link" onClick={() => setPanel(panel === "outbox" ? null : "outbox")}>
              {panel === "outbox" ? "Ocultar registro de envíos" : "Registro de envíos"}
            </button>
          </div>
          {panel === "mappings" && <MappingsEditor key={mappingsKey} provider={s.provider} readOnly={!isAdmin} />}
          {panel === "outbox" && <OutboxLog provider={s.provider} canRetry={isAdmin} />}
        </div>
      )}
      <ErrorBox error={error} />
      {odooModal && (
        <OdooModal
          onClose={() => setOdooModal(false)}
          onSaved={(next) => {
            setOdooModal(false);
            onChange(next);
          }}
        />
      )}
      {tokenModal && (
        <TokenModal
          onClose={() => setTokenModal(false)}
          onSaved={(next) => {
            setTokenModal(false);
            onChange(next);
          }}
        />
      )}
    </Card>
  );
}

function OdooModal({ onClose, onSaved }: { onClose: () => void; onSaved: (s: CRMStatus) => void }) {
  const [form, setForm] = useState({ url: "https://", db: "", login: "", api_key: "" });
  const [run, busy, error] = useAction();
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  async function save() {
    const r = await run(() => send<CRMStatus>("/api/integrations/odoo/credentials", "POST", form));
    if (r) onSaved(r);
  }
  return (
    <Modal
      title="Conectar Odoo"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !form.db || !form.login || !form.api_key} onClick={save}>
            {busy ? "Validando…" : "Conectar"}
          </button>
        </>
      }
    >
      <div className="form">
        <p className="small muted" style={{ margin: 0 }}>
          En Odoo: Preferencias del usuario → Seguridad de la cuenta → Nueva clave de API. El usuario necesita acceso a
          Contactos y CRM. La clave se guarda cifrada en la bóveda.
        </p>
        <Field label="URL de Odoo">
          <input value={form.url} onChange={set("url")} placeholder="https://miempresa.odoo.com" />
        </Field>
        <Field label="Base de datos">
          <input value={form.db} onChange={set("db")} placeholder="miempresa" />
        </Field>
        <Field label="Usuario (correo)">
          <input value={form.login} onChange={set("login")} />
        </Field>
        <Field label="Clave de API">
          <input type="password" autoComplete="off" value={form.api_key} onChange={set("api_key")} />
        </Field>
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}

function TokenModal({ onClose, onSaved }: { onClose: () => void; onSaved: (s: CRMStatus) => void }) {
  const [token, setToken] = useState("");
  const [run, busy, error] = useAction();
  async function save() {
    const r = await run(() => send<CRMStatus>("/api/integrations/hubspot/token", "POST", { token: token.trim() }));
    if (r) onSaved(r);
  }
  return (
    <Modal
      title="Conectar HubSpot con token de app privada"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !token.trim()} onClick={save}>
            Conectar
          </button>
        </>
      }
    >
      <div className="form">
        <p className="small muted" style={{ margin: 0 }}>
          En HubSpot: Configuración → Integraciones → Apps privadas → Crear. Activa los permisos de contactos, negocios y
          notas (crm.objects.contacts, crm.objects.deals, lectura y escritura). El token se guarda cifrado en la bóveda; no
          vuelve a mostrarse.
        </p>
        <Field label="Token de acceso">
          <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="pat-…" />
        </Field>
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}

function OutboxLog({ provider, canRetry }: { provider: CRMProvider; canRetry: boolean }) {
  const [filter, setFilter] = useState("");
  const rows = useApi<OutboxRow[]>(`/api/integrations/${provider}/outbox${qs({ status: filter, limit: 100 })}`);
  const [run, busy, error] = useAction();
  async function retry(id: number) {
    if (await run(() => send(`/api/integrations/${provider}/outbox/${id}/retry`, "POST"))) rows.reload();
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="inline">
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Estado">
          <option value="">Todos</option>
          <option value="pending">Pendientes</option>
          <option value="failed">Fallidos</option>
          <option value="sent">Enviados</option>
          <option value="skipped">Sin cambios</option>
        </select>
        <button className="link small" onClick={rows.reload}>
          Actualizar
        </button>
      </div>
      <ErrorBox error={error ?? rows.error} />
      {!rows.data ? (
        <Loading />
      ) : !rows.data.length ? (
        <p className="muted small">Sin envíos.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Tipo</th>
                <th>Estado</th>
                <th className="num">Intentos</th>
                <th>Detalle</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.data.map((o) => (
                <tr key={o.id}>
                  <td className="nowrap small">{fmtDateTime(o.sent_at ?? o.created_at)}</td>
                  <td className="small">
                    {ENTITY_LABEL[o.entity_type] ?? o.entity_type} #{o.entity_id}
                  </td>
                  <td>
                    <Badge tone={o.status === "sent" ? "ok" : o.status === "failed" ? "bad" : o.status === "pending" ? "warn" : "neutral"}>
                      {OUTBOX_STATUS_LABEL[o.status] ?? o.status}
                    </Badge>
                  </td>
                  <td className="num">{o.attempts}</td>
                  <td className="small" style={{ maxWidth: 360 }}>
                    {o.error ?? (o.status === "pending" ? `Próximo intento ${fmtDateTime(o.next_attempt_at)}` : "—")}
                  </td>
                  <td className="right">
                    {canRetry && (o.status === "failed" || o.status === "pending") && (
                      <button className="link small" disabled={busy} onClick={() => retry(o.id)}>
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
    </div>
  );
}

type HubSpotAttributionResult = { created: string[]; existing: string[]; mapped: number | string[] | boolean };
type SalesforceAttributionResult = { mapped: string[]; missing: string[] };

/** Envía el origen de la conversación (canal, UTMs, campaña, enlace, gclid) al CRM. */
function AttributionSync({ provider, onMapped }: { provider: CRMProvider; onMapped: () => void }) {
  const [run, busy, error] = useAction();
  const [hubspot, setHubspot] = useState<HubSpotAttributionResult | null>(null);
  const [salesforce, setSalesforce] = useState<SalesforceAttributionResult | null>(null);

  async function go() {
    if (provider === "hubspot") {
      const r = await run(() => send<HubSpotAttributionResult>("/api/integrations/hubspot/attribution-properties", "POST"));
      if (r) {
        setHubspot(r);
        onMapped();
      }
    } else {
      const r = await run(() => send<SalesforceAttributionResult>("/api/integrations/salesforce/attribution-mapping", "POST"));
      if (r) {
        setSalesforce(r);
        onMapped();
      }
    }
  }
  const mappedCount = (m: HubSpotAttributionResult["mapped"]) => (Array.isArray(m) ? m.length : typeof m === "number" ? m : m ? "sí" : 0);

  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="inline" style={{ justifyContent: "space-between" }}>
        <span className="small">
          <strong>Atribución en el CRM:</strong>{" "}
          {provider === "hubspot"
            ? "crea las propiedades de origen de WhatsApp (canal, UTMs, campaña, anuncio, mensaje disparador, gclid) y las mapea."
            : "mapea el origen de WhatsApp a LeadSource y a los campos personalizados que existan en tu organización."}
        </span>
        <button disabled={busy} onClick={go}>
          {busy ? "Procesando…" : provider === "hubspot" ? "Crear propiedades de atribución" : "Mapear atribución"}
        </button>
      </div>
      <ErrorBox error={error} />
      {hubspot && (
        <p className="small" style={{ margin: 0 }}>
          ✅ {hubspot.created.length} propiedades creadas, {hubspot.existing.length} ya existían · mapeadas: {mappedCount(hubspot.mapped)}.
        </p>
      )}
      {salesforce && (
        <div className="small">
          ✅ Mapeados: {salesforce.mapped.length ? salesforce.mapped.join(", ") : "ninguno"}.
          {salesforce.missing.length > 0 && (
            <>
              {" "}
              Para enviar el resto, crea estos campos personalizados de texto en Salesforce (Contacto y Lead) y vuelve a pulsar el
              botón: <code>{salesforce.missing.join(", ")}</code>
            </>
          )}
        </div>
      )}
    </div>
  );
}
