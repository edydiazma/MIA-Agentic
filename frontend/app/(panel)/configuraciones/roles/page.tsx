"use client";

import { useState } from "react";
import { send, type AgentDetail } from "@/lib/api";
import { SCOPE_LABEL, usePermissions, type PermissionCatalog, type RoleOut } from "@/lib/security";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";

type Draft = { name: string; description: string; base_role: string; data_scope: string; permissions: Set<string> };

function RoleEditor({ role, catalog, onClose }: { role: RoleOut | null; catalog: PermissionCatalog; onClose: () => void }) {
  const [d, setD] = useState<Draft>({
    name: role?.name ?? "",
    description: role?.description ?? "",
    base_role: role?.base_role ?? "agent",
    data_scope: role?.data_scope ?? "own",
    permissions: new Set(role?.permissions ?? []),
  });
  const [run, busy, err] = useAction();
  const toggle = (k: string) => {
    const p = new Set(d.permissions);
    if (p.has(k)) p.delete(k);
    else p.add(k);
    setD({ ...d, permissions: p });
  };
  async function save() {
    const body = { ...d, permissions: [...d.permissions] };
    const r = await run(() => (role ? send(`/api/roles/${role.id}`, "PUT", body) : send("/api/roles", "POST", body)));
    if (r) onClose();
  }
  return (
    <Modal title={role ? `Editar rol «${role.name}»` : "Nuevo rol"} onClose={onClose} wide
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy} onClick={save}>Guardar</button></>}>
      <div className="grid2">
        <Field label="Nombre"><input value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} required /></Field>
        <Field label="Descripción"><input value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} /></Field>
        <Field label="Rol base" hint="Define el comportamiento heredado (bandeja, asignación). Solo un administrador puede crear roles con base administrador.">
          <select value={d.base_role} disabled={role?.is_system} onChange={(e) => setD({ ...d, base_role: e.target.value })}>
            <option value="agent">Asesor</option>
            <option value="supervisor">Supervisor</option>
            <option value="admin">Administrador</option>
          </select>
        </Field>
        <Field label="Alcance de los datos" hint="Qué conversaciones, clientes y reportes puede ver">
          <select value={d.data_scope} onChange={(e) => setD({ ...d, data_scope: e.target.value })}>
            {catalog.scopes.map((s) => <option key={s} value={s}>{SCOPE_LABEL[s] ?? s}</option>)}
          </select>
        </Field>
      </div>
      <div className="grid2" style={{ marginTop: 12 }}>
        {catalog.groups.map((g) => {
          const all = g.permissions.every((p) => d.permissions.has(p.key));
          return (
            <fieldset key={g.group} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 10 }}>
              <legend className="inline">
                <strong>{g.group}</strong>
                <button type="button" className="link small" onClick={() => {
                  const p = new Set(d.permissions);
                  g.permissions.forEach((x) => (all ? p.delete(x.key) : p.add(x.key)));
                  setD({ ...d, permissions: p });
                }}>{all ? "Quitar todos" : "Todos"}</button>
              </legend>
              {g.permissions.map((p) => (
                <label key={p.key} className="inline small" style={{ display: "flex" }}>
                  <input type="checkbox" checked={d.permissions.has(p.key)} onChange={() => toggle(p.key)} />
                  {p.label}
                </label>
              ))}
            </fieldset>
          );
        })}
      </div>
      <ErrorBox error={err} />
    </Modal>
  );
}

function Assignments({ roles }: { roles: RoleOut[] }) {
  const agents = useApi<(AgentDetail & { role_id?: number | null })[]>("/api/agents");
  const [run, , err] = useAction();
  const byKey = Object.fromEntries(roles.filter((r) => r.is_system).map((r) => [r.key, r.id]));
  return (
    <Card title="Usuarios y roles">
      <ErrorBox error={err ?? agents.error} />
      {!agents.data ? <Loading /> : (
        <table className="table">
          <thead><tr><th>Usuario</th><th>Rol</th></tr></thead>
          <tbody>
            {agents.data.filter((a) => a.is_active).map((a) => (
              <tr key={a.id}>
                <td>{a.name} <span className="muted small">{a.email}</span></td>
                <td>
                  <select aria-label={`Rol de ${a.name}`} value={String(a.role_id ?? byKey[a.role] ?? "")}
                    onChange={async (e) => {
                      await run(() => send(`/api/roles/assign/${a.id}`, "PUT", { role_id: Number(e.target.value) }));
                      agents.reload();
                    }}>
                    {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

export default function RolesPage() {
  const { can, loading } = usePermissions();
  const roles = useApi<RoleOut[]>("/api/roles");
  const catalog = useApi<PermissionCatalog>("/api/roles/catalog");
  const [editing, setEditing] = useState<RoleOut | null | "new">(null);
  const [run, , err] = useAction();
  const manage = can("roles.manage");

  return (
    <>
      <PageHeader title="Roles y permisos" subtitle="Qué puede ver y hacer cada usuario. Los roles del sistema se pueden ajustar; el de administrador tiene todo." />
      <ConfigTabs />
      <ErrorBox error={err ?? roles.error} />
      {loading || !roles.data || !catalog.data ? <Loading /> : (
        <>
          <Card title="Roles" actions={manage && <button className="primary" onClick={() => setEditing("new")}>Nuevo rol</button>}>
            {roles.data.length === 0 ? <Empty>Sin roles</Empty> : (
              <table className="table">
                <thead><tr><th>Rol</th><th>Alcance</th><th>Permisos</th><th>Usuarios</th><th /></tr></thead>
                <tbody>
                  {roles.data.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <strong>{r.name}</strong> {r.is_system && <Badge>Sistema</Badge>}
                        {r.description && <div className="small muted">{r.description}</div>}
                      </td>
                      <td>{SCOPE_LABEL[r.data_scope]}</td>
                      <td>{r.locked ? "Todos" : r.permissions.length}</td>
                      <td>{r.users}</td>
                      <td className="inline">
                        {manage && !r.locked && <button onClick={() => setEditing(r)}>Editar</button>}
                        {manage && <button onClick={async () => { await run(() => send(`/api/roles/${r.id}/duplicate`, "POST")); roles.reload(); }}>Duplicar</button>}
                        {manage && !r.is_system && (
                          <button className="danger" onClick={async () => {
                            if (confirm(`¿Eliminar el rol «${r.name}»? Sus usuarios pasan al rol del sistema equivalente.`)) {
                              await run(() => send(`/api/roles/${r.id}`, "DELETE"));
                              roles.reload();
                            }
                          }}>Eliminar</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
          {can("users.manage") && <Assignments roles={roles.data} />}
        </>
      )}
      {editing && catalog.data && (
        <RoleEditor role={editing === "new" ? null : editing} catalog={catalog.data}
          onClose={() => { setEditing(null); roles.reload(); }} />
      )}
    </>
  );
}
