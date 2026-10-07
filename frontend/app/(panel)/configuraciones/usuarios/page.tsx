"use client";

import { useState } from "react";
import { AVAILABILITY_LABEL, send, type AgentDetail, type Availability, type Group } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Tabs, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

type UserDraft = {
  id?: number;
  name: string;
  email: string;
  password: string;
  role: "admin" | "agent";
  group_ids: number[];
  is_active: boolean;
  availability: Availability;
};
type GroupDraft = { id?: number; name: string; description: string };

function GroupChecks({ groups, value, onChange }: { groups: Group[]; value: number[]; onChange: (v: number[]) => void }) {
  if (!groups.length) return <span className="muted small">No hay grupos creados.</span>;
  return (
    <div className="inline">
      {groups.map((g) => (
        <label key={g.id} className="inline small">
          <input
            type="checkbox"
            checked={value.includes(g.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, g.id] : value.filter((x) => x !== g.id))}
          />
          {g.name}
        </label>
      ))}
    </div>
  );
}

export default function UsuariosPage() {
  const isAdmin = useIsAdmin();
  const [tab, setTab] = useState<"users" | "groups">("users");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const groups = useApi<Group[]>("/api/groups");
  const [user, setUser] = useState<UserDraft | null>(null);
  const [group, setGroup] = useState<GroupDraft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  const groupList = groups.data ?? [];
  const groupName = (id: number) => groupList.find((g) => g.id === id)?.name ?? `#${id}`;

  async function saveUser() {
    if (!user) return;
    const ok = await run(() =>
      user.id
        ? send(`/api/agents/${user.id}`, "PUT", {
            name: user.name, role: user.role, group_ids: user.group_ids, is_active: user.is_active,
            availability: user.availability, ...(user.password ? { password: user.password } : {}),
          })
        : send("/api/agents", "POST", {
            name: user.name, email: user.email, password: user.password, role: user.role, group_ids: user.group_ids,
          }),
    );
    if (ok) {
      setUser(null);
      agents.reload();
    }
  }

  async function saveGroup() {
    if (!group) return;
    const body = { name: group.name, description: group.description || null };
    const ok = await run(() => (group.id ? send(`/api/groups/${group.id}`, "PUT", body) : send("/api/groups", "POST", body)));
    if (ok) {
      setGroup(null);
      groups.reload();
    }
  }

  async function removeGroup(g: Group) {
    if (!confirm(`¿Eliminar el grupo «${g.name}»? Las conversaciones del grupo quedarán sin grupo.`)) return;
    await run(() => send(`/api/groups/${g.id}`, "DELETE"));
    groups.reload();
    agents.reload();
  }

  const members = (gid: number) => (agents.data ?? []).filter((a) => a.group_ids.includes(gid));

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Gestión de usuarios: asesores, administradores y grupos." />
      <ConfigTabs />
      <AdminNotice />
      <Tabs value={tab} onChange={setTab} tabs={[["users", "Usuarios"], ["groups", "Grupos"]]} />
      <ErrorBox error={!user && !group ? actionError : null} />

      {tab === "users" ? (
        <Card
          actions={
            isAdmin && (
              <button
                className="primary"
                onClick={() => {
                  setActionError(null);
                  setUser({ name: "", email: "", password: "", role: "agent", group_ids: [], is_active: true, availability: "available" });
                }}
              >
                Nuevo usuario
              </button>
            )
          }
        >
          <ErrorBox error={agents.error} />
          {agents.loading && !agents.data ? (
            <Loading />
          ) : !agents.data?.length ? (
            <Empty>Sin usuarios.</Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>Nombre</th><th>Correo</th><th>Rol</th><th>Grupos</th><th>Conexión</th><th>Estado</th><th /></tr>
                </thead>
                <tbody>
                  {agents.data.map((a) => (
                    <tr key={a.id}>
                      <td className="strong">{a.name}</td>
                      <td className="small">{a.email}</td>
                      <td>{a.role === "admin" ? <Badge tone="info">Administrador</Badge> : <Badge>Asesor</Badge>}</td>
                      <td className="small">{a.group_ids.map(groupName).join(", ") || <span className="muted">—</span>}</td>
                      <td className="small nowrap">
                        <span className="inline">
                          <span className={`dot ${a.online ? "on" : ""}`} />
                          {a.online ? AVAILABILITY_LABEL[a.availability] : "Desconectado"}
                        </span>
                      </td>
                      <td>{a.is_active ? <Badge tone="ok">Activo</Badge> : <Badge tone="bad">Inactivo</Badge>}</td>
                      <td>
                        {isAdmin && (
                          <button
                            onClick={() => {
                              setActionError(null);
                              setUser({
                                id: a.id, name: a.name, email: a.email, password: "", role: a.role,
                                group_ids: a.group_ids, is_active: a.is_active, availability: a.availability,
                              });
                            }}
                          >
                            Editar
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
      ) : (
        <Card
          actions={isAdmin && (
            <button className="primary" onClick={() => { setActionError(null); setGroup({ name: "", description: "" }); }}>Nuevo grupo</button>
          )}
        >
          <p className="small muted" style={{ marginTop: 0 }}>
            Los grupos (ventas, posventa, cartera…) reciben transferencias del bot y de las tareas automatizadas.
          </p>
          <ErrorBox error={groups.error} />
          {groups.loading && !groups.data ? (
            <Loading />
          ) : !groupList.length ? (
            <Empty>Sin grupos.</Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Grupo</th><th>Descripción</th><th>Integrantes</th><th /></tr></thead>
                <tbody>
                  {groupList.map((g) => (
                    <tr key={g.id}>
                      <td className="strong">{g.name}</td>
                      <td className="small muted">{g.description ?? "—"}</td>
                      <td className="small">{members(g.id).map((a) => a.name).join(", ") || <span className="muted">Sin integrantes</span>}</td>
                      <td className="nowrap">
                        {isAdmin && (
                          <div className="inline">
                            <button onClick={() => { setActionError(null); setGroup({ id: g.id, name: g.name, description: g.description ?? "" }); }}>Editar</button>
                            <button className="danger" onClick={() => removeGroup(g)} disabled={busy}>Eliminar</button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {user && (
        <Modal
          title={user.id ? "Editar usuario" : "Nuevo usuario"}
          onClose={() => setUser(null)}
          footer={
            <>
              <button onClick={() => setUser(null)}>Cancelar</button>
              <button
                className="primary"
                onClick={saveUser}
                disabled={busy || !user.name.trim() || (!user.id && (!user.email.trim() || user.password.length < 8))}
              >
                Guardar
              </button>
            </>
          }
        >
          <Field label="Nombre"><input value={user.name} onChange={(e) => setUser({ ...user, name: e.target.value })} /></Field>
          <Field label="Correo">
            <input type="email" value={user.email} disabled={!!user.id} onChange={(e) => setUser({ ...user, email: e.target.value })} />
          </Field>
          <Field label={user.id ? "Nueva contraseña (opcional)" : "Contraseña"} hint="Mínimo 8 caracteres.">
            <input type="password" value={user.password} onChange={(e) => setUser({ ...user, password: e.target.value })} />
          </Field>
          <div className="grid2">
            <Field label="Rol">
              <select value={user.role} onChange={(e) => setUser({ ...user, role: e.target.value as UserDraft["role"] })}>
                <option value="agent">Asesor</option>
                <option value="admin">Administrador</option>
              </select>
            </Field>
            {user.id && (
              <Field label="Disponibilidad">
                <select value={user.availability} onChange={(e) => setUser({ ...user, availability: e.target.value as Availability })}>
                  {(Object.keys(AVAILABILITY_LABEL) as Availability[]).map((a) => (
                    <option key={a} value={a}>{AVAILABILITY_LABEL[a]}</option>
                  ))}
                </select>
              </Field>
            )}
          </div>
          <Field label="Grupos">
            <GroupChecks groups={groupList} value={user.group_ids} onChange={(v) => setUser({ ...user, group_ids: v })} />
          </Field>
          {user.id && (
            <Toggle checked={user.is_active} onChange={(v) => setUser({ ...user, is_active: v })} label={user.is_active ? "Activo" : "Inactivo (no puede ingresar)"} />
          )}
          <ErrorBox error={actionError} />
        </Modal>
      )}

      {group && (
        <Modal
          title={group.id ? "Editar grupo" : "Nuevo grupo"}
          onClose={() => setGroup(null)}
          footer={
            <>
              <button onClick={() => setGroup(null)}>Cancelar</button>
              <button className="primary" onClick={saveGroup} disabled={busy || !group.name.trim()}>Guardar</button>
            </>
          }
        >
          <Field label="Nombre"><input value={group.name} onChange={(e) => setGroup({ ...group, name: e.target.value })} /></Field>
          <Field label="Descripción" hint="El bot la usa para elegir a qué grupo transferir.">
            <input value={group.description} onChange={(e) => setGroup({ ...group, description: e.target.value })} />
          </Field>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
