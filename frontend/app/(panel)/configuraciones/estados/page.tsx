"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { useRealtime } from "@/lib/realtime";
import { fmtDuration, type AgentStatusDef, type StatusBoardRow } from "@/lib/ops-types";

type Draft = Omit<AgentStatusDef, "id" | "key" | "is_system"> & { id?: number; key?: string; is_system?: boolean };

const EMPTY: Draft = {
  name: "", icon: "🟣", color: "#7c3aed", receives_conversations: false, counts_as_working: true,
  is_default: false, is_offline: false, position: 50, is_active: true,
};

export default function EstadosPage() {
  const isAdmin = useIsAdmin();
  const list = useApi<AgentStatusDef[]>("/api/agent-statuses");
  const board = useApi<StatusBoardRow[]>("/api/agents/status-board");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  useRealtime((event) => { if (event === "agent.status") board.reload(); });

  async function save() {
    if (!draft) return;
    const { id, is_system, key, ...body } = draft;
    const ok = await run(() => (id ? send(`/api/agent-statuses/${id}`, "PUT", body)
                                   : send("/api/agent-statuses", "POST", { ...body, key: key || undefined })));
    if (ok) { setDraft(null); list.reload(); }
  }

  async function remove(s: AgentStatusDef) {
    if (!confirm(`¿Eliminar el estado «${s.name}»?`)) return;
    await run(() => send(`/api/agent-statuses/${s.id}`, "DELETE"));
    list.reload();
  }

  async function setAgentStatus(agentId: number, statusId: number) {
    await run(() => send(`/api/agents/${agentId}/status`, "PUT", { status_id: statusId }));
    board.reload();
  }

  const statuses = list.data ?? [];

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Estados de asesor: disponibilidad, pausas y tiempo en cada estado." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={list.error || (!draft ? actionError : null)} />

      <Card
        title="Estados"
        actions={isAdmin && <button className="primary" onClick={() => { setActionError(null); setDraft({ ...EMPTY }); }}>Nuevo estado</button>}
      >
        <p className="muted small">
          Solo los estados que <strong>reciben conversaciones</strong> entran en la asignación automática. El tiempo
          en cada estado alimenta el reporte de ingreso (Login). Los estados del sistema no se pueden eliminar.
        </p>
        {list.loading && !list.data ? <Loading /> : !statuses.length ? <Empty>Sin estados.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Estado</th><th>Recibe conversaciones</th><th>Cuenta como trabajo</th><th>Rol</th><th /></tr>
              </thead>
              <tbody>
                {statuses.map((s) => (
                  <tr key={s.id} style={{ opacity: s.is_active ? 1 : 0.55 }}>
                    <td>
                      <span aria-hidden style={{ color: s.color ?? undefined }}>{s.icon}</span> <strong>{s.name}</strong>{" "}
                      <span className="muted small">{s.key}</span>
                    </td>
                    <td>{s.receives_conversations ? <Badge tone="ok">Sí</Badge> : <Badge>No</Badge>}</td>
                    <td>{s.counts_as_working ? "Sí" : "No"}</td>
                    <td className="small">
                      {s.is_default && <Badge tone="info">Al ingresar</Badge>} {s.is_offline && <Badge>Al salir</Badge>}
                      {s.is_system && <span className="muted"> Sistema</span>}
                    </td>
                    <td className="right">
                      {isAdmin && (
                        <span className="inline">
                          <button className="small" onClick={() => { setActionError(null); setDraft({ ...s }); }}>Editar</button>
                          {!s.is_system && !s.is_default && !s.is_offline && (
                            <button className="small danger" disabled={busy} onClick={() => remove(s)}>Eliminar</button>
                          )}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Tablero de estados" actions={<button className="small" onClick={() => board.reload()}>Actualizar</button>}>
        {board.loading && !board.data ? <Loading /> : !(board.data ?? []).length ? <Empty>Sin asesores.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Asesor</th><th>Estado</th><th>Tiempo en el estado</th><th>Abiertas</th><th>Conexión</th><th /></tr>
              </thead>
              <tbody>
                {(board.data ?? []).map((r) => (
                  <tr key={r.agent_id}>
                    <td><strong>{r.name}</strong> <span className="muted small">{r.employee_code ?? ""}</span></td>
                    <td>
                      <span aria-hidden style={{ color: r.status?.color ?? undefined }}>{r.status?.icon}</span> {r.status?.name ?? "—"}
                    </td>
                    <td>{fmtDuration(r.seconds_in_status)}</td>
                    <td>{r.open_conversations}</td>
                    <td>{r.online ? <Badge tone="ok">En línea</Badge> : <Badge>Sin conexión</Badge>}</td>
                    <td className="right">
                      {isAdmin && (
                        <select
                          aria-label={`Cambiar estado de ${r.name}`}
                          value={r.status?.id ?? ""}
                          onChange={(e) => setAgentStatus(r.agent_id, Number(e.target.value))}
                          style={{ width: "auto" }}
                        >
                          {statuses.filter((s) => s.is_active).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                        </select>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {draft && (
        <Modal
          title={draft.id ? "Editar estado" : "Nuevo estado"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.name.trim()}>Guardar</button>
            </>
          }
        >
          <div className="grid2">
            <Field label="Nombre"><input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Field>
            <Field label="Ícono (emoji)"><input value={draft.icon ?? ""} onChange={(e) => setDraft({ ...draft, icon: e.target.value })} /></Field>
          </div>
          <div className="grid2">
            <Field label="Color"><input type="color" value={draft.color ?? "#7c3aed"} onChange={(e) => setDraft({ ...draft, color: e.target.value })} /></Field>
            <Field label="Orden"><input type="number" value={draft.position} onChange={(e) => setDraft({ ...draft, position: Number(e.target.value) })} /></Field>
          </div>
          <Toggle checked={draft.receives_conversations} label="Recibe conversaciones (entra en la asignación automática)"
            onChange={(v) => setDraft({ ...draft, receives_conversations: v })} />
          <Toggle checked={draft.counts_as_working} label="Cuenta como tiempo laborado"
            onChange={(v) => setDraft({ ...draft, counts_as_working: v })} />
          <Toggle checked={draft.is_default} label="Estado al ingresar" onChange={(v) => setDraft({ ...draft, is_default: v, is_offline: v ? false : draft.is_offline })} />
          <Toggle checked={draft.is_active} label="Activo" onChange={(v) => setDraft({ ...draft, is_active: v })} />
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
