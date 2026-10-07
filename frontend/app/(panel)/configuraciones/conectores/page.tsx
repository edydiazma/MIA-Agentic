"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { ConnectionRow } from "@/components/hub/HubConnections";
import type { ConnectorDefinition, HubConnection } from "@/components/hub/types";

const AUTH_SECRETS: Record<string, string[]> = {
  none: [],
  api_key: ["api_key"],
  bearer: ["api_key"],
  basic: ["username", "password"],
  oauth2_client_credentials: ["client_id", "client_secret"],
};
const SECRET_LABEL: Record<string, string> = {
  api_key: "Clave de API / token",
  username: "Usuario",
  password: "Contraseña",
  client_id: "Client ID",
  client_secret: "Client secret",
  webhook_secret: "Secreto de webhooks",
};

type Draft = { name: string; description: string; base_url: string; auth: string; endpoints: string; mappings: string; webhooks: string; is_published: boolean };

const toDraft = (d?: ConnectorDefinition): Draft => ({
  name: d?.name ?? "",
  description: d?.description ?? "",
  base_url: d?.base_url ?? "https://",
  auth: JSON.stringify(d?.auth ?? { type: "api_key", header: "X-API-Key" }, null, 2),
  endpoints: JSON.stringify(d?.endpoints ?? [], null, 2),
  mappings: JSON.stringify(d?.mappings ?? {}, null, 2),
  webhooks: JSON.stringify(d?.webhooks ?? {}, null, 2),
  is_published: d?.is_published ?? false,
});

function parseDraft(d: Draft) {
  return {
    name: d.name,
    description: d.description || null,
    base_url: d.base_url,
    auth: JSON.parse(d.auth || "{}"),
    endpoints: JSON.parse(d.endpoints || "[]"),
    mappings: JSON.parse(d.mappings || "{}"),
    webhooks: JSON.parse(d.webhooks || "{}"),
    is_published: d.is_published,
  };
}

export default function ConectoresPage() {
  const isAdmin = useIsAdmin();
  const defs = useApi<ConnectorDefinition[]>("/api/hub/connectors");
  const conns = useApi<HubConnection[]>("/api/hub/connections");
  const [editing, setEditing] = useState<{ def?: ConnectorDefinition; template?: ConnectorDefinition } | null>(null);

  const own = (defs.data ?? []).filter((d) => !d.template);
  const templates = (defs.data ?? []).filter((d) => d.template);

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Conectores propios: conecta el DMS, ERP u otro sistema de tu empresa por su API REST, sin programar."
        actions={isAdmin && <button className="primary" onClick={() => setEditing({})}>Nuevo conector</button>}
      />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={defs.error ?? conns.error} />
      {!defs.data || !conns.data ? (
        <Loading />
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          <Card title="Plantillas">
            <p className="muted small" style={{ marginTop: 0 }}>
              Parte de una plantilla y ajusta la URL, los endpoints y el mapeo a tu sistema.
            </p>
            <div className="grid2">
              {templates.map((t) => (
                <div key={t.id} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
                  <strong>{t.name}</strong>
                  <p className="small muted">{t.description}</p>
                  {isAdmin && <button onClick={() => setEditing({ template: t })}>Usar plantilla</button>}
                </div>
              ))}
            </div>
          </Card>
          {own.length === 0 ? (
            <Empty>Aún no tienes conectores propios.</Empty>
          ) : (
            own.map((d) => (
              <ConnectorCard
                key={d.id}
                def={d}
                conns={conns.data!.filter((c) => c.connector_id === d.id)}
                onEdit={() => setEditing({ def: d })}
                reload={() => {
                  defs.reload();
                  conns.reload();
                }}
              />
            ))
          )}
        </div>
      )}
      {editing && (
        <ConnectorEditor
          def={editing.def}
          template={editing.template}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            defs.reload();
          }}
        />
      )}
    </>
  );
}

function ConnectorCard({ def, conns, onEdit, reload }: { def: ConnectorDefinition; conns: HubConnection[]; onEdit: () => void; reload: () => void }) {
  const isAdmin = useIsAdmin();
  const [run, busy, error] = useAction();
  const [adding, setAdding] = useState(false);
  const [testing, setTesting] = useState<HubConnection | null>(null);

  async function remove() {
    if (!confirm(`¿Eliminar el conector ${def.name}?`)) return;
    if (await run(() => send(`/api/hub/connectors/${def.id}`, "DELETE"))) reload();
  }

  return (
    <Card
      title={
        <span className="inline">
          {def.name} <Badge tone={def.is_published ? "ok" : "neutral"}>{def.is_published ? "Publicado" : "Borrador"}</Badge>
          <span className="small muted">v{def.version}</span>
        </span>
      }
      actions={
        isAdmin && (
          <>
            <button onClick={onEdit}>Editar</button>
            <button className="primary" onClick={() => setAdding(true)}>
              Conectar
            </button>
            <button className="danger" disabled={busy || conns.length > 0} onClick={remove}>
              Eliminar
            </button>
          </>
        )
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        {def.description} <code>{def.base_url}</code> · {def.endpoints.length} endpoints · entidades:{" "}
        {Object.keys(def.mappings).join(", ") || "—"}
      </p>
      {conns.length === 0 ? (
        <Empty>Sin conexiones.</Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {conns.map((c) => (
            <div key={c.id}>
              <ConnectionRow conn={c} reload={reload} />
              {isAdmin && (
                <button className="link small" onClick={() => setTesting(c)}>
                  Probar endpoint
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <ErrorBox error={error} />
      {adding && (
        <ConnectModal
          def={def}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            reload();
          }}
        />
      )}
      {testing && <TestModal def={def} conn={testing} onClose={() => setTesting(null)} />}
    </Card>
  );
}

function ConnectorEditor({
  def,
  template,
  onClose,
  onSaved,
}: {
  def?: ConnectorDefinition;
  template?: ConnectorDefinition;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => {
    const d = toDraft(def ?? template);
    return template ? { ...d, name: `${template.name} (copia)` } : d;
  });
  const [errors, setErrors] = useState<string[]>([]);
  const [run, busy, error] = useAction();
  const set = (k: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setDraft({ ...draft, [k]: e.target.value });

  function body() {
    try {
      return parseDraft(draft);
    } catch (e) {
      setErrors([`JSON inválido: ${(e as Error).message}`]);
      return null;
    }
  }
  async function validate() {
    const b = body();
    if (!b) return;
    const r = await run(() => send<{ errors: string[] }>("/api/hub/connectors/validate", "POST", b));
    if (r) setErrors(r.errors.length ? r.errors : ["✅ La definición es válida."]);
  }
  async function save() {
    const b = body();
    if (!b) return;
    const r = await run(() => (def ? send(`/api/hub/connectors/${def.id}`, "PUT", b) : send("/api/hub/connectors", "POST", b)));
    if (r) onSaved();
  }

  return (
    <Modal
      wide
      title={def ? `Editar ${def.name}` : "Nuevo conector"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button disabled={busy} onClick={validate}>
            Validar
          </button>
          <button className="primary" disabled={busy || !draft.name} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <div className="form">
        <div className="grid2">
          <Field label="Nombre">
            <input value={draft.name} onChange={set("name")} />
          </Field>
          <Field label="URL base (https)">
            <input value={draft.base_url} onChange={set("base_url")} />
          </Field>
        </div>
        <Field label="Descripción">
          <input value={draft.description} onChange={set("description")} />
        </Field>
        <Field label="Autenticación" hint='type: none | api_key (header o query_param) | bearer | basic | oauth2_client_credentials (token_url, scopes). Los secretos se cargan al conectar.'>
          <textarea rows={4} className="mono" value={draft.auth} onChange={set("auth")} />
        </Field>
        <Field
          label="Endpoints"
          hint="[{key, method, path, query, body, items_path, pagination: {type: none|page|offset|cursor|link, param, size, size_param, limit_param, next_path}}]. Plantillas: {{since}}, {{page}}, {{id}}, {{email}}, {{phone}}."
        >
          <textarea rows={10} className="mono" value={draft.endpoints} onChange={set("endpoints")} />
        </Field>
        <Field
          label="Mapeo"
          hint='{contact|product|order: {pull: {endpoint, id: "$.id", fields: {campo: "$.ruta" | {path, transforms: ["trim","lower","phone_e164",{"map":{…}},{"concat":["$.a","$.b"]},{"date":"%d/%m/%Y"}]}}}, push: {endpoint, id_path}}, deal: {push}, find_contact: {endpoint, id_path}}'
        >
          <textarea rows={12} className="mono" value={draft.mappings} onChange={set("mappings")} />
        </Field>
        <Field label="Webhooks" hint='{secret_header, signature: hmac_sha256|token, encoding: hex|base64, entity: order|contact|product, items_path}'>
          <textarea rows={4} className="mono" value={draft.webhooks} onChange={set("webhooks")} />
        </Field>
        <Toggle checked={draft.is_published} onChange={(v) => setDraft({ ...draft, is_published: v })} label="Publicado" />
        {errors.length > 0 && (
          <ul className="small" style={{ margin: 0 }}>
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        )}
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}

function ConnectModal({ def, onClose, onSaved }: { def: ConnectorDefinition; onClose: () => void; onSaved: () => void }) {
  const authType = String((def.auth as { type?: string }).type ?? "none");
  const names = [...(AUTH_SECRETS[authType] ?? []), ...(Object.keys(def.webhooks).length ? ["webhook_secret"] : [])];
  const [label, setLabel] = useState("");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [crmPush, setCrmPush] = useState(false);
  const canPush = Boolean((def.mappings as Record<string, { push?: unknown }>).contact?.push);
  const [run, busy, error] = useAction();
  async function save() {
    const r = await run(() =>
      send(`/api/hub/connectors/${def.id}/connections`, "POST", { label, secrets, settings: { crm_push: crmPush } }),
    );
    if (r) onSaved();
  }
  return (
    <Modal
      title={`Conectar ${def.name}`}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !label} onClick={save}>
            Conectar
          </button>
        </>
      }
    >
      <div className="form">
        <Field label="Nombre de la conexión" hint="P. ej. «DMS sede norte». Puedes conectar varias instancias.">
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </Field>
        {names.map((n) => (
          <Field key={n} label={SECRET_LABEL[n] ?? n}>
            <input
              type={n === "username" ? "text" : "password"}
              autoComplete="off"
              value={secrets[n] ?? ""}
              onChange={(e) => setSecrets({ ...secrets, [n]: e.target.value })}
            />
          </Field>
        ))}
        {canPush && (
          <Toggle checked={crmPush} onChange={setCrmPush} label="Enviar clientes y negocios a este sistema (cola de sincronización del CRM)" />
        )}
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}

function TestModal({ def, conn, onClose }: { def: ConnectorDefinition; conn: HubConnection; onClose: () => void }) {
  const [endpoint, setEndpoint] = useState(def.endpoints[0]?.key ?? "");
  const [entity, setEntity] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const [run, busy, error] = useAction();
  async function go() {
    const r = await run(() => send(`/api/hub/connections/${conn.id}/test`, "POST", { endpoint, entity: entity || null }));
    if (r) setResult(r);
  }
  return (
    <Modal wide title={`Probar endpoint — ${conn.label}`} onClose={onClose} footer={<button onClick={onClose}>Cerrar</button>}>
      <div className="form">
        <div className="grid2">
          <Field label="Endpoint">
            <select value={endpoint} onChange={(e) => setEndpoint(e.target.value)}>
              {def.endpoints.map((e) => (
                <option key={e.key} value={e.key}>
                  {e.key} ({e.method ?? "GET"} {e.path})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Ver mapeado como">
            <select value={entity} onChange={(e) => setEntity(e.target.value)}>
              <option value="">— solo respuesta —</option>
              {Object.keys(def.mappings)
                .filter((k) => ["contact", "product", "order"].includes(k))
                .map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        <p className="small muted" style={{ margin: 0 }}>
          Trae solo la primera página (máx. 5 registros). Las credenciales aparecen enmascaradas.
        </p>
        <button className="primary" disabled={busy || !endpoint} onClick={go}>
          {busy ? "Probando…" : "Probar"}
        </button>
        {result != null && (
          <pre className="mono small" style={{ maxHeight: 360, overflow: "auto" }}>
            {JSON.stringify(result, null, 2)}
          </pre>
        )}
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
