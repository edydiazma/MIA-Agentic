"use client";

import { useEffect, useState } from "react";
import { fmtDateTime, send } from "@/lib/api";
import {
  usePermissions,
  type RoleOut,
  type SecurityPolicy,
  type SSOConnection,
} from "@/lib/security";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Stat, Toggle, useAction, useApi } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";

type Overview = {
  users: number;
  mfa_enabled: number;
  must_change_password: number;
  sso_users: number;
  locked: { id: number; name: string; email: string; locked_until: string }[];
};

function PolicyCard() {
  const { data, error, reload } = useApi<SecurityPolicy>("/api/security/policy");
  const [form, setForm] = useState<SecurityPolicy | null>(null);
  const [ips, setIps] = useState("");
  const [run, busy, err] = useAction();
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (data) {
      setForm(data);
      setIps(data.allowed_ips.join("\n"));
    }
  }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!form) return <Loading />;
  const set = <K extends keyof SecurityPolicy>(k: K, v: SecurityPolicy[K]) => setForm({ ...form, [k]: v });
  const num = (k: keyof SecurityPolicy, label: string, hint?: string) => (
    <Field label={label} hint={hint}>
      <input type="number" value={form[k] as number} onChange={(e) => set(k, Number(e.target.value) as never)} />
    </Field>
  );

  async function save() {
    setSaved(false);
    const r = await run(() => send("/api/security/policy", "PUT", {
      ...form, allowed_ips: ips.split(/[\n,]/).map((x) => x.trim()).filter(Boolean),
    }));
    if (r) {
      setSaved(true);
      reload();
    }
  }

  return (
    <Card title="Política de acceso" actions={<button className="primary" disabled={busy} onClick={save}>Guardar</button>}>
      <div className="grid2">
        <div style={{ display: "grid", gap: 10 }}>
          <h3>Contraseñas</h3>
          {num("min_length", "Longitud mínima", "Entre 8 y 128")}
          <Toggle checked={form.require_upper} onChange={(v) => set("require_upper", v)} label="Exigir mayúscula" />
          <Toggle checked={form.require_lower} onChange={(v) => set("require_lower", v)} label="Exigir minúscula" />
          <Toggle checked={form.require_digit} onChange={(v) => set("require_digit", v)} label="Exigir número" />
          <Toggle checked={form.require_symbol} onChange={(v) => set("require_symbol", v)} label="Exigir símbolo" />
          {num("expiry_days", "Vencimiento (días)", "0 = no vence")}
          {num("history", "No repetir las últimas", "Cantidad de contraseñas anteriores")}
          <Toggle checked={form.breached_check} onChange={(v) => set("breached_check", v)}
            label="Rechazar contraseñas filtradas (Have I Been Pwned, consulta anónima)" />
        </div>
        <div style={{ display: "grid", gap: 10 }}>
          <h3>Ingreso</h3>
          {num("lockout_attempts", "Bloquear tras intentos fallidos")}
          {num("lockout_minutes", "Minutos de bloqueo")}
          <Field label="Segundo factor (2FA) obligatorio">
            <select value={form.mfa_required} onChange={(e) => set("mfa_required", e.target.value as SecurityPolicy["mfa_required"])}>
              <option value="none">Opcional</option>
              <option value="admins">Solo administradores</option>
              <option value="all">Todos los usuarios</option>
            </select>
          </Field>
          {num("trusted_device_days", "Recordar dispositivos (días)", "0 = pedir el código siempre")}
          {num("session_timeout_minutes", "Duración de la sesión (minutos)")}
          <Field label="IPs o rangos permitidos" hint="Uno por línea, formato CIDR (ej. 190.24.10.0/24). Vacío = cualquier red. Tu IP actual debe quedar incluida.">
            <textarea rows={4} value={ips} onChange={(e) => setIps(e.target.value)} placeholder="190.24.10.0/24" />
          </Field>
        </div>
      </div>
      <ErrorBox error={err} />
      {saved && <p className="small" role="status">Política guardada.</p>}
    </Card>
  );
}

const EMPTY_SSO = {
  protocol: "saml" as "saml" | "oidc",
  name: "",
  slug: "",
  idp_entity_id: "",
  idp_sso_url: "",
  idp_certificate: "",
  idp_metadata_url: "",
  issuer: "",
  client_id: "",
  client_secret: "",
  domains: "",
  jit_provisioning: true,
  default_role_id: "" as string | number,
  group_claim: "",
  group_mapping: "{}",
  enforce: false,
  is_active: false,
};

function SsoEditor({ conn, roles, onClose }: { conn: SSOConnection | null; roles: RoleOut[]; onClose: () => void }) {
  const [f, setF] = useState(() =>
    conn
      ? {
          ...EMPTY_SSO,
          ...Object.fromEntries(Object.entries(conn).map(([k, v]) => [k, v ?? ""])),
          domains: conn.domains.join(", "),
          group_mapping: JSON.stringify(conn.group_mapping ?? {}, null, 2),
          default_role_id: conn.default_role_id ?? "",
          client_secret: "",
        }
      : EMPTY_SSO,
  );
  const [run, busy, err, setErr] = useAction();
  const set = (k: keyof typeof EMPTY_SSO, v: unknown) => setF({ ...f, [k]: v });
  const text = (k: keyof typeof EMPTY_SSO, label: string, hint?: string, multiline = false) => (
    <Field label={label} hint={hint}>
      {multiline ? (
        <textarea rows={4} value={String(f[k] ?? "")} onChange={(e) => set(k, e.target.value)} />
      ) : (
        <input value={String(f[k] ?? "")} onChange={(e) => set(k, e.target.value)} />
      )}
    </Field>
  );

  async function save() {
    let mapping: unknown;
    try {
      mapping = JSON.parse(String(f.group_mapping || "{}"));
    } catch {
      setErr("El mapeo de grupos debe ser JSON válido");
      return;
    }
    const body = {
      ...f,
      domains: String(f.domains).split(",").map((d) => d.trim()).filter(Boolean),
      group_mapping: mapping,
      default_role_id: f.default_role_id === "" ? null : Number(f.default_role_id),
      client_secret: f.client_secret || null,
      slug: f.slug || null,
    };
    const r = await run(() => (conn ? send(`/api/security/sso/${conn.id}`, "PUT", body) : send("/api/security/sso", "POST", body)));
    if (r) onClose();
  }

  return (
    <Modal title={conn ? `Editar ${conn.name}` : "Nueva conexión SSO"} onClose={onClose} wide
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy} onClick={save}>Guardar</button></>}>
      <div className="grid2">
        <Field label="Protocolo">
          <select value={f.protocol} onChange={(e) => set("protocol", e.target.value)} disabled={!!conn}>
            <option value="saml">SAML 2.0 (OneLogin, Okta, Azure AD, ADFS)</option>
            <option value="oidc">OpenID Connect (Google, Azure AD, Okta, Auth0)</option>
          </select>
        </Field>
        {text("name", "Nombre", "Ej. OneLogin")}
        {!conn && text("slug", "Identificador (opcional)", "Se usa en las URL; letras, números y guiones")}
        {text("domains", "Dominios de correo", "Separados por coma. Los usuarios con esos correos verán «Ingresar con SSO».")}
      </div>
      {f.protocol === "saml" ? (
        <div className="grid2">
          {text("idp_metadata_url", "URL de metadata del proveedor", "Si la indicas, completamos entityID, URL y certificado")}
          {text("idp_entity_id", "Entity ID del proveedor (Issuer)")}
          {text("idp_sso_url", "URL de inicio de sesión (SSO, HTTP-Redirect)")}
          {text("idp_certificate", "Certificado X.509 del proveedor (PEM)", undefined, true)}
        </div>
      ) : (
        <div className="grid2">
          {text("issuer", "Emisor (issuer)", "Ej. https://acme.onelogin.com/oidc/2")}
          {text("client_id", "Client ID")}
          <Field label="Client secret" hint={conn?.has_client_secret ? "Guardado en la bóveda. Déjalo vacío para conservarlo." : "Se guarda cifrado en la bóveda"}>
            <input type="password" value={String(f.client_secret)} onChange={(e) => set("client_secret", e.target.value)} autoComplete="off" />
          </Field>
        </div>
      )}
      <div className="grid2">
        <Toggle checked={!!f.jit_provisioning} onChange={(v) => set("jit_provisioning", v)} label="Crear el usuario en su primer ingreso (JIT)" />
        <Field label="Rol por defecto">
          <select value={String(f.default_role_id)} onChange={(e) => set("default_role_id", e.target.value)}>
            <option value="">Asesor</option>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </Field>
        {text("group_claim", "Atributo / claim de grupos", "Ej. groups, memberOf")}
        {text("group_mapping", "Mapeo de grupos (JSON)", '{"Ventas": {"role_key": "agent", "group_ids": [3]}}', true)}
        <Toggle checked={!!f.enforce} onChange={(v) => set("enforce", v)} label="Exigir SSO para estos dominios (sin contraseña local)" />
        <Toggle checked={!!f.is_active} onChange={(v) => set("is_active", v)} label="Activa" />
      </div>
      {conn && (
        <Card title="Datos para configurar en tu proveedor">
          <dl className="small" style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", margin: 0 }}>
            {conn.protocol === "saml" ? (
              <>
                <dt>Entity ID (SP)</dt><dd><code>{conn.sp.entity_id}</code></dd>
                <dt>ACS URL</dt><dd><code>{conn.sp.acs_url}</code></dd>
                <dt>Metadata</dt><dd><code>{conn.sp.metadata_url}</code></dd>
              </>
            ) : (
              <><dt>Redirect URI</dt><dd><code>{conn.sp.oidc_redirect_uri}</code></dd></>
            )}
            <dt>Ingreso directo</dt><dd><code>{conn.sp.login_url}</code></dd>
          </dl>
        </Card>
      )}
      <ErrorBox error={err} />
    </Modal>
  );
}

function SsoCard() {
  const { data, error, reload } = useApi<SSOConnection[]>("/api/security/sso");
  const roles = useApi<RoleOut[]>("/api/roles");
  const [editing, setEditing] = useState<SSOConnection | null | "new">(null);
  const [tests, setTests] = useState<Record<number, { ok: boolean; detail: string }>>({});
  const [run] = useAction();
  return (
    <Card title="Inicio de sesión único (SSO)" actions={<button onClick={() => setEditing("new")}>Nueva conexión</button>}>
      <ErrorBox error={error} />
      {!data ? <Loading /> : data.length === 0 ? (
        <Empty>Conecta OneLogin, Okta, Azure AD o Google para que tu equipo ingrese con su cuenta corporativa.</Empty>
      ) : (
        <table className="table">
          <thead><tr><th>Conexión</th><th>Dominios</th><th>Estado</th><th>Último ingreso</th><th /></tr></thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id}>
                <td><strong>{c.name}</strong> <span className="muted small">{c.protocol.toUpperCase()}</span></td>
                <td className="small">{c.domains.join(", ") || "—"}</td>
                <td>
                  {c.is_active ? <Badge tone="ok">Activa</Badge> : <Badge>Inactiva</Badge>}{" "}
                  {c.enforce && <Badge tone="info">Obligatoria</Badge>}
                  {tests[c.id] && <div className="small" role="status">{tests[c.id].ok ? "✓ " : "✗ "}{tests[c.id].detail}</div>}
                </td>
                <td className="small">{c.last_login_at ? fmtDateTime(c.last_login_at) : "—"}</td>
                <td className="inline">
                  <button onClick={async () => {
                    const r = await run(() => send<{ ok: boolean; detail: string }>(`/api/security/sso/${c.id}/test`, "POST"));
                    if (r) setTests({ ...tests, [c.id]: r });
                  }}>Probar</button>
                  <button onClick={() => setEditing(c)}>Editar</button>
                  <button className="danger" onClick={async () => {
                    if (confirm(`¿Eliminar la conexión ${c.name}?`)) {
                      await run(() => send(`/api/security/sso/${c.id}`, "DELETE"));
                      reload();
                    }
                  }}>Eliminar</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && (
        <SsoEditor conn={editing === "new" ? null : editing} roles={roles.data ?? []}
          onClose={() => { setEditing(null); reload(); }} />
      )}
    </Card>
  );
}

function OverviewCard() {
  const { data, reload } = useApi<Overview>("/api/security/overview");
  const [run] = useAction();
  if (!data) return null;
  return (
    <Card title="Resumen">
      <div className="stats">
        <Stat label="Usuarios activos" value={data.users} />
        <Stat label="Con 2FA" value={`${data.mfa_enabled} / ${data.users}`} />
        <Stat label="Deben cambiar contraseña" value={data.must_change_password} />
        <Stat label="Solo SSO" value={data.sso_users} />
      </div>
      {data.locked.length > 0 && (
        <>
          <h3>Cuentas bloqueadas</h3>
          <table className="table">
            <tbody>
              {data.locked.map((a) => (
                <tr key={a.id}>
                  <td>{a.name} <span className="muted small">{a.email}</span></td>
                  <td className="small">hasta {fmtDateTime(a.locked_until)}</td>
                  <td><button onClick={async () => { await run(() => send(`/api/security/users/${a.id}/unlock`, "POST")); reload(); }}>Desbloquear</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Card>
  );
}

export default function SecuritySettingsPage() {
  const { can, loading } = usePermissions();
  return (
    <>
      <PageHeader title="Seguridad" subtitle="Política de contraseñas, segundo factor, redes permitidas e inicio de sesión único." />
      <ConfigTabs />
      {loading ? <Loading /> : !can("security.manage") ? (
        <Empty>No tienes permiso para administrar la seguridad.</Empty>
      ) : (
        <>
          <OverviewCard />
          <PolicyCard />
          <SsoCard />
        </>
      )}
    </>
  );
}
