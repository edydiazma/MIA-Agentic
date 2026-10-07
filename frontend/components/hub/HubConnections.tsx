"use client";

import { useState } from "react";
import { api, fmtDateTime, send, timeAgo } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, Toggle, useAction, useApi } from "@/components/ui";
import { useIsAdmin } from "@/components/config/common";
import type { HubConnection, HubProvider, HubRun } from "./types";

const STORE_HELP: Record<string, string> = {
  shopify:
    "Shopify: Configuración → Apps → Desarrollar apps → crea una app con permisos read_products, read_orders y read_customers; copia el token de Admin API (shpat_…) y el secreto de la app (para validar los webhooks).",
  woocommerce:
    "WooCommerce: Ajustes → Avanzado → API REST → Agregar clave (lectura). Para los webhooks (Pedido creado / actualizado) usa la URL que verás al conectar y un secreto propio.",
  vtex: "VTEX: crea una llave de aplicación (appKey / appToken) con rol de lectura de OMS y Catálogo. El hook de pedidos se registra con la URL y el encabezado X-Hub-Secret que verás al conectar.",
};
const FIELD_LABEL: Record<string, string> = {
  shop: "Tienda (tienda.myshopify.com)",
  access_token: "Token de Admin API",
  webhook_secret: "Secreto de webhooks",
  url: "URL de la tienda (https://)",
  consumer_key: "Consumer key",
  consumer_secret: "Consumer secret",
  account: "Cuenta (accountName)",
  environment: "Ambiente",
  app_key: "App key",
  app_token: "App token",
};
const SECRET_FIELDS = new Set(["access_token", "webhook_secret", "consumer_key", "consumer_secret", "app_key", "app_token"]);

function statusBadge(c: HubConnection) {
  if (c.status === "connected") return <Badge tone="ok">Conectado</Badge>;
  if (c.status === "error") return <Badge tone="bad">Error</Badge>;
  return <Badge>{c.status}</Badge>;
}

/** Tiendas en línea y calendarios (se monta en Configuraciones → Integraciones). */
export default function HubConnections() {
  const isAdmin = useIsAdmin();
  const providers = useApi<HubProvider[]>("/api/hub/providers");
  const conns = useApi<HubConnection[]>("/api/hub/connections");
  const [adding, setAdding] = useState<HubProvider | null>(null);
  const [run, busy, error] = useAction();

  async function oauth(p: HubProvider, shop?: string, label?: string) {
    const q = shop ? `?shop=${encodeURIComponent(shop)}&label=${encodeURIComponent(label ?? shop)}` : "";
    const r = await run(() => api<{ url: string }>(`/api/hub/oauth/${p.provider}/start${q}`));
    if (r) window.location.href = r.url;
  }

  if (!providers.data || !conns.data) return <Loading />;
  const stores = providers.data.filter((p) => p.kind === "commerce");
  const cals = providers.data.filter((p) => p.kind === "calendar");
  const list = conns.data.filter((c) => c.provider !== "custom");

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title="Tiendas en línea"
        actions={
          isAdmin && (
            <>
              {stores.map((p) => (
                <button key={p.provider} onClick={() => setAdding(p)}>
                  + {p.label}
                </button>
              ))}
            </>
          )
        }
      >
        <p className="muted small" style={{ marginTop: 0 }}>
          Trae el catálogo y los pedidos de cada tienda, enlaza el pedido con el cliente (teléfono o correo) y con la
          conversación de WhatsApp de los días previos; los pedidos pagados quedan como productos comprados del cliente.
          Puedes conectar varias tiendas del mismo tipo con nombres distintos.
        </p>
        <ConnectionList items={list.filter((c) => stores.some((s) => s.provider === c.provider))} reload={conns.reload} />
      </Card>

      <Card title="Calendarios">
        <p className="muted small" style={{ marginTop: 0 }}>
          Publica las citas (crear, mover, cancelar) en Google Calendar u Outlook, bloquea los horarios ocupados al
          ofrecer citas y, si un evento se cancela o se mueve en el calendario, la cita cambia igual.
        </p>
        <div className="inline" style={{ marginBottom: 12 }}>
          {cals.map((p) => {
            const connected = list.some((c) => c.provider === p.provider);
            return (
              <button
                key={p.provider}
                className={connected ? undefined : "primary"}
                disabled={!isAdmin || busy || !p.oauth || connected}
                title={!p.oauth ? "Falta configurar la app OAuth en el servidor" : undefined}
                onClick={() => oauth(p)}
              >
                {connected ? `${p.label} conectado` : `Conectar ${p.label}`}
              </button>
            );
          })}
        </div>
        <ConnectionList items={list.filter((c) => cals.some((s) => s.provider === c.provider))} reload={conns.reload} />
      </Card>
      <ErrorBox error={error} />
      {adding && (
        <StoreModal
          provider={adding}
          onClose={() => setAdding(null)}
          onOAuth={(shop, label) => oauth(adding, shop, label)}
          onSaved={() => {
            setAdding(null);
            conns.reload();
          }}
        />
      )}
    </div>
  );
}

function ConnectionList({ items, reload }: { items: HubConnection[]; reload: () => void }) {
  if (!items.length) return <Empty>Sin conexiones.</Empty>;
  return (
    <div className="stack" style={{ gap: 12 }}>
      {items.map((c) => (
        <ConnectionRow key={c.id} conn={c} reload={reload} />
      ))}
    </div>
  );
}

export function ConnectionRow({ conn: c, reload }: { conn: HubConnection; reload: () => void }) {
  const isAdmin = useIsAdmin();
  const [run, busy, error] = useAction();
  const [open, setOpen] = useState(false);
  const isCal = c.provider.endsWith("_calendar");
  const settings = c.settings as Record<string, unknown>;

  async function save(patch: Record<string, unknown>) {
    if (await run(() => send(`/api/hub/connections/${c.id}`, "PUT", patch))) reload();
  }
  async function syncNow(full = false) {
    if (await run(() => send(`/api/hub/connections/${c.id}/sync?full=${full}`, "POST"))) setTimeout(reload, 1500);
  }
  async function remove() {
    if (!confirm(`¿Desconectar ${c.label ?? c.provider_label}? Se borran las credenciales; los pedidos ya traídos se conservan hasta borrar la conexión.`))
      return;
    if (await run(() => send(`/api/hub/connections/${c.id}`, "DELETE"))) reload();
  }

  return (
    <div className="card-inner" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
      <div className="inline" style={{ justifyContent: "space-between" }}>
        <span className="inline">
          <strong>{c.label ?? c.provider_label}</strong>
          <span className="muted small">{c.provider_label}</span>
          {statusBadge(c)}
        </span>
        {isAdmin && (
          <span className="inline">
            <button disabled={busy} onClick={() => syncNow()}>
              {busy ? "…" : "Sincronizar ahora"}
            </button>
            <button className="link" onClick={() => setOpen(!open)}>
              {open ? "Ocultar" : "Detalle"}
            </button>
            <button className="danger" disabled={busy} onClick={remove}>
              Desconectar
            </button>
          </span>
        )}
      </div>
      <div className="small muted" style={{ marginTop: 6 }}>
        Última sincronización: <span title={fmtDateTime(c.last_sync_at)}>{timeAgo(c.last_sync_at)}</span>
        {c.orders != null && <> · {c.orders} pedidos</>}
        {c.last_run && (
          <>
            {" "}
            · última corrida ({c.last_run.entity}): {c.last_run.fetched} leídos, {c.last_run.created} nuevos, {c.last_run.updated}{" "}
            actualizados{c.last_run.failed ? `, ${c.last_run.failed} con error` : ""}
          </>
        )}
      </div>
      {c.last_error && <div className="error-box">{c.last_error}</div>}
      {open && (
        <div className="stack" style={{ gap: 10, marginTop: 10 }}>
          {c.webhook_url && (
            <Field label="URL de webhook (regístrala en la tienda)" hint={c.provider === "vtex" ? "Encabezado: X-Hub-Secret = tu secreto de webhooks" : undefined}>
              <input readOnly value={c.webhook_url} onFocus={(e) => e.target.select()} />
            </Field>
          )}
          <Toggle checked={c.sync_enabled} onChange={(v) => isAdmin && save({ sync_enabled: v })} label="Sincronización automática" />
          {isCal ? (
            <>
              <Toggle
                checked={settings.push_appointments !== false}
                onChange={(v) => isAdmin && save({ settings: { push_appointments: v } })}
                label="Publicar las citas en el calendario"
              />
              <Toggle
                checked={settings.busy_blocks_slots !== false}
                onChange={(v) => isAdmin && save({ settings: { busy_blocks_slots: v } })}
                label="Los ocupados del calendario bloquean horarios de citas"
              />
              <CalendarMapping conn={c} onSave={(calendars) => save({ settings: { calendars } })} />
            </>
          ) : (
            <div className="grid2">
              <Toggle
                checked={settings.sync_catalog !== false}
                onChange={(v) => isAdmin && save({ settings: { sync_catalog: v } })}
                label="Traer el catálogo de productos"
              />
              <Field label="Ventana de atribución (días)" hint="Un pedido se atribuye a la conversación de WhatsApp de estos días previos.">
                <input
                  type="number"
                  min={1}
                  max={180}
                  defaultValue={Number(settings.attribution_days ?? 30)}
                  disabled={!isAdmin}
                  onBlur={(e) => save({ settings: { attribution_days: Number(e.target.value) || 30 } })}
                />
              </Field>
            </div>
          )}
          <RunLog connId={c.id} />
          {isAdmin && !isCal && (
            <button className="link small" onClick={() => syncNow(true)}>
              Resincronizar todo (ignora el cursor)
            </button>
          )}
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

function CalendarMapping({ conn, onSave }: { conn: HubConnection; onSave: (calendars: Record<string, unknown>) => void }) {
  const isAdmin = useIsAdmin();
  const current = (conn.settings.calendars as { default?: string; agents?: Record<string, string> }) ?? {};
  const [def, setDef] = useState(current.default ?? "primary");
  const [agents, setAgents] = useState(
    Object.entries(current.agents ?? {})
      .map(([a, cal]) => `${a}=${cal}`)
      .join("\n"),
  );
  function save() {
    const map: Record<string, string> = {};
    for (const line of agents.split("\n")) {
      const [a, cal] = line.split("=").map((x) => x?.trim());
      if (a && cal) map[a] = cal;
    }
    onSave({ default: def.trim() || "primary", agents: map });
  }
  return (
    <div className="grid2">
      <Field label="Calendario por defecto" hint='"primary" = calendario principal de la cuenta conectada.'>
        <input value={def} disabled={!isAdmin} onChange={(e) => setDef(e.target.value)} />
      </Field>
      <Field label="Calendario por asesor" hint="Una línea por asesor: id_asesor=id_calendario (p. ej. 12=ventas@empresa.com).">
        <textarea rows={3} value={agents} disabled={!isAdmin} onChange={(e) => setAgents(e.target.value)} />
      </Field>
      {isAdmin && (
        <div>
          <button onClick={save}>Guardar calendarios</button>
        </div>
      )}
    </div>
  );
}

export function RunLog({ connId }: { connId: number }) {
  const runs = useApi<HubRun[]>(`/api/hub/connections/${connId}/runs?limit=10`);
  if (!runs.data) return <Loading />;
  if (!runs.data.length) return <Empty>Aún no hay corridas.</Empty>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Inicio</th>
          <th>Entidad</th>
          <th>Dirección</th>
          <th>Estado</th>
          <th>Leídos / nuevos / act. / errores</th>
        </tr>
      </thead>
      <tbody>
        {runs.data.map((r) => (
          <tr key={r.id} title={r.error ?? r.sample_errors.map((e) => `${e.ref}: ${e.error}`).join("\n")}>
            <td className="small">{fmtDateTime(r.started_at)}</td>
            <td>{r.entity}</td>
            <td>{r.direction}</td>
            <td>
              <Badge tone={r.status === "succeeded" ? "ok" : r.status === "running" ? "info" : r.status === "partial" ? "warn" : "bad"}>
                {r.status}
              </Badge>
            </td>
            <td className="small">
              {r.fetched} / {r.created} / {r.updated} / {r.failed}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StoreModal({
  provider,
  onClose,
  onSaved,
  onOAuth,
}: {
  provider: HubProvider;
  onClose: () => void;
  onSaved: () => void;
  onOAuth: (shop: string, label: string) => void;
}) {
  const [form, setForm] = useState<Record<string, string>>({ label: "", environment: "vtexcommercestable" });
  const [run, busy, error] = useAction();
  const fields = provider.fields ?? [];
  async function save() {
    if (await run(() => send(`/api/hub/commerce/${provider.provider}`, "POST", form))) onSaved();
  }
  return (
    <Modal
      title={`Conectar ${provider.label}`}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          {provider.provider === "shopify" && provider.oauth && (
            <button disabled={!form.shop || !form.label} onClick={() => onOAuth(form.shop, form.label)}>
              Instalar la app (OAuth)
            </button>
          )}
          <button className="primary" disabled={busy || !form.label} onClick={save}>
            {busy ? "Validando…" : "Conectar"}
          </button>
        </>
      }
    >
      <div className="form">
        <p className="small muted" style={{ margin: 0 }}>
          {STORE_HELP[provider.provider]}
        </p>
        <Field label="Nombre de la tienda" hint="Para distinguirla si conectas varias (p. ej. «Tienda Bogotá»).">
          <input value={form.label ?? ""} onChange={(e) => setForm({ ...form, label: e.target.value })} />
        </Field>
        {fields.map((f) =>
          f === "environment" ? (
            <Field key={f} label={FIELD_LABEL[f]}>
              <select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
                <option value="vtexcommercestable">vtexcommercestable</option>
                <option value="vtexcommercebeta">vtexcommercebeta</option>
              </select>
            </Field>
          ) : (
            <Field key={f} label={FIELD_LABEL[f] ?? f}>
              <input
                type={SECRET_FIELDS.has(f) ? "password" : "text"}
                autoComplete="off"
                value={form[f] ?? ""}
                onChange={(e) => setForm({ ...form, [f]: e.target.value })}
              />
            </Field>
          ),
        )}
        {provider.provider === "vtex" && (
          <Field label="Secreto de webhooks (X-Hub-Secret)">
            <input type="password" value={form.webhook_secret ?? ""} onChange={(e) => setForm({ ...form, webhook_secret: e.target.value })} />
          </Field>
        )}
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
