"use client";

import { useEffect, useState } from "react";
import { send, type Agent, type Channel, type Group } from "@/lib/api";
import { Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, useIsAdmin } from "@/components/config/common";
import { ROUTING_LABELS, type GroupSettings, type Routing, type RoutingSettings } from "@/lib/ops-types";

export default function EnrutamientoPage() {
  const isAdmin = useIsAdmin();
  const settings = useApi<RoutingSettings>("/api/settings/routing");
  const groups = useApi<GroupSettings[]>("/api/groups/settings");
  const allGroups = useApi<Group[]>("/api/groups").data ?? [];
  const channels = useApi<{ channels: Channel[] }>("/api/channels").data?.channels ?? [];
  const agents = useApi<Agent[]>("/api/agents").data ?? [];
  const [draft, setDraft] = useState<RoutingSettings | null>(null);
  const [edit, setEdit] = useState<GroupSettings | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => { if (settings.data && !draft) setDraft(settings.data); }, [settings.data, draft]);

  async function saveSettings() {
    if (!draft) return;
    setSaved(null);
    if (await run(() => send("/api/settings/routing", "PUT", draft))) { setSaved("Guardado"); settings.reload(); }
  }

  async function saveGroup() {
    if (!edit) return;
    const { group_id, name, members, ...body } = edit;
    if (await run(() => send(`/api/groups/${group_id}/settings`, "PUT", body))) { setEdit(null); groups.reload(); }
  }

  const name = (id: number) => agents.find((a) => a.id === id)?.name ?? `#${id}`;
  const toggleIn = (list: number[] | null, id: number, on: boolean) => {
    const base = list ?? [];
    return on ? [...base, id] : base.filter((x) => x !== id);
  };

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Enrutamiento: cómo se asignan las conversaciones a los asesores." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={settings.error || groups.error || (!edit ? actionError : null)} />

      <Card title="Reglas generales">
        {!draft ? <Loading /> : (
          <div className="form" style={{ maxWidth: 640 }}>
            <Toggle checked={draft.sticky_agent} label="Reasignar al mismo asesor si está disponible"
              onChange={(v) => isAdmin && setDraft({ ...draft, sticky_agent: v })} />
            <Toggle checked={draft.owner_on_first_assignment} label="El primer asesor asignado queda como dueño del cliente"
              onChange={(v) => isAdmin && setDraft({ ...draft, owner_on_first_assignment: v })} />
            <Toggle checked={draft.assign_when_none_available} label="En horario, si nadie está disponible, asignar igual a un integrante del grupo"
              onChange={(v) => isAdmin && setDraft({ ...draft, assign_when_none_available: v })} />
            <div className="grid2">
              <Field label="Cierre de sesión por inactividad (min)" hint="Sin actividad del panel pasa a desconectado.">
                <input type="number" min={1} value={draft.session_timeout_minutes} disabled={!isAdmin}
                  onChange={(e) => setDraft({ ...draft, session_timeout_minutes: Number(e.target.value) })} />
              </Field>
              <Field label="Espera al cerrar la última pestaña (s)">
                <input type="number" min={0} value={draft.offline_grace_seconds} disabled={!isAdmin}
                  onChange={(e) => setDraft({ ...draft, offline_grace_seconds: Number(e.target.value) })} />
              </Field>
            </div>
            {isAdmin && <SaveBar onSave={saveSettings} busy={busy} saved={saved} error={null} />}
          </div>
        )}
      </Card>

      <Card title="Por grupo">
        {groups.loading && !groups.data ? <Loading /> : !(groups.data ?? []).length ? <Empty>No hay grupos.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Grupo</th><th>Estrategia</th><th>Máx. abiertas</th><th>Transferir a</th><th>Canales</th><th>Supervisores</th><th /></tr>
              </thead>
              <tbody>
                {(groups.data ?? []).map((g) => (
                  <tr key={g.group_id}>
                    <td><strong>{g.name}</strong> <span className="muted small">{g.members.length} integrantes</span></td>
                    <td>{ROUTING_LABELS[g.routing]}</td>
                    <td>{g.max_open_per_agent ?? "—"}</td>
                    <td className="small">
                      {g.transfer_group_ids == null ? "Cualquier grupo"
                        : g.transfer_group_ids.length ? g.transfer_group_ids.map((id) => allGroups.find((x) => x.id === id)?.name).join(", ")
                        : "Ninguno"}
                    </td>
                    <td className="small">
                      {g.channel_ids.length ? g.channel_ids.map((id) => channels.find((c) => c.id === id)?.name ?? `#${id}`).join(", ") : "Todos"}
                    </td>
                    <td className="small">{g.supervisor_ids.map(name).join(", ") || "—"}</td>
                    <td className="right">{isAdmin && <button className="small" onClick={() => { setActionError(null); setEdit({ ...g }); }}>Editar</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {edit && (
        <Modal
          title={`Enrutamiento · ${edit.name}`}
          onClose={() => setEdit(null)}
          footer={<><button onClick={() => setEdit(null)}>Cancelar</button><button className="primary" disabled={busy} onClick={saveGroup}>Guardar</button></>}
        >
          <Field label="Estrategia de asignación">
            <select value={edit.routing} onChange={(e) => setEdit({ ...edit, routing: e.target.value as Routing })}>
              {(Object.keys(ROUTING_LABELS) as Routing[]).map((r) => <option key={r} value={r}>{ROUTING_LABELS[r]}</option>)}
            </select>
          </Field>
          <Field label="Máximo de conversaciones abiertas por asesor" hint="Vacío = sin límite.">
            <input type="number" min={1} value={edit.max_open_per_agent ?? ""}
              onChange={(e) => setEdit({ ...edit, max_open_per_agent: e.target.value ? Number(e.target.value) : null })} />
          </Field>
          <Field label="Grupos a los que se puede transferir" hint="Los asesores solo pueden transferir a estos grupos (administradores y supervisores sin límite).">
            <Toggle checked={edit.transfer_group_ids == null} label="A cualquier grupo"
              onChange={(v) => setEdit({ ...edit, transfer_group_ids: v ? null : [] })} />
            {edit.transfer_group_ids != null && (
              <div className="inline" style={{ flexWrap: "wrap" }}>
                {allGroups.filter((g) => g.id !== edit.group_id).map((g) => (
                  <label key={g.id} className="inline small">
                    <input type="checkbox" checked={edit.transfer_group_ids!.includes(g.id)}
                      onChange={(e) => setEdit({ ...edit, transfer_group_ids: toggleIn(edit.transfer_group_ids, g.id, e.target.checked) })} />
                    {g.name}
                  </label>
                ))}
              </div>
            )}
          </Field>
          <Field label="Canales que atiende" hint="Ninguno marcado = todos los canales.">
            <div className="inline" style={{ flexWrap: "wrap" }}>
              {channels.map((c) => (
                <label key={c.id} className="inline small">
                  <input type="checkbox" checked={edit.channel_ids.includes(c.id)}
                    onChange={(e) => setEdit({ ...edit, channel_ids: toggleIn(edit.channel_ids, c.id, e.target.checked) })} />
                  {c.name}
                </label>
              ))}
            </div>
          </Field>
          <Field label="Supervisores del grupo" hint="Ven el tablero y reciben avisos de SLA de este grupo.">
            <div className="inline" style={{ flexWrap: "wrap" }}>
              {edit.members.map((id) => (
                <label key={id} className="inline small">
                  <input type="checkbox" checked={edit.supervisor_ids.includes(id)}
                    onChange={(e) => setEdit({ ...edit, supervisor_ids: toggleIn(edit.supervisor_ids, id, e.target.checked) })} />
                  {name(id)}
                </label>
              ))}
              {!edit.members.length && <span className="muted small">El grupo no tiene integrantes.</span>}
            </div>
          </Field>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
