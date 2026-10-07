"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  type AgentDetail,
  CHANNEL_ICONS,
  STATUS_LABEL,
  contactLabel,
  downloadUrl,
  fmtNum,
  qs,
  send,
  timeAgo,
} from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Stat, useAction, useApi } from "@/components/ui";
import type { BulkAssignResult, MonitoringItem, MonitoringResponse, SupervisedGroups } from "@/lib/supervision-types";

type Filters = {
  status: string;
  group_id: string;
  agent_id: string;
  channel_id: string;
  waiting_min_gte: string;
  stuck_in_bot_min: string;
  unattended: boolean;
  sla_breached: boolean;
  tag: string;
  sort: string;
};

const EMPTY: Filters = {
  status: "open", group_id: "", agent_id: "", channel_id: "", waiting_min_gte: "", stuck_in_bot_min: "",
  unattended: false, sla_breached: false, tag: "", sort: "wait",
};

const PRESETS: { label: string; f: Partial<Filters> }[] = [
  { label: "Todas abiertas", f: {} },
  { label: "En cola sin asesor", f: { status: "unassigned" } },
  { label: "Esperando > 10 min", f: { waiting_min_gte: "10" } },
  { label: "Sin atender", f: { unattended: true } },
  { label: "SLA vencido", f: { sla_breached: true } },
  { label: "Estancadas en bot > 30 min", f: { stuck_in_bot_min: "30", status: "bot" } },
];

function waitTone(min: number | null): "ok" | "warn" | "bad" | "neutral" {
  if (min == null) return "neutral";
  if (min >= 15) return "bad";
  if (min >= 5) return "warn";
  return "ok";
}

export default function MonitoreoPage() {
  const [f, setF] = useState<Filters>(EMPTY);
  const params = useMemo(() => {
    const p: Record<string, string | number | boolean | null> = { status: f.status, sort: f.sort };
    for (const k of ["group_id", "agent_id", "channel_id", "waiting_min_gte", "stuck_in_bot_min", "tag"] as const) {
      if (f[k]) p[k] = f[k];
    }
    if (f.unattended) p.unattended = true;
    if (f.sla_breached) p.sla_breached = true;
    return p;
  }, [f]);
  const list = useApi<MonitoringResponse>(`/api/monitoring/conversations${qs(params)}`);
  const meta = useApi<SupervisedGroups>("/api/monitoring/supervised-groups");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [target, setTarget] = useState<{ agent_id: string; group_id: string }>({ agent_id: "", group_id: "" });
  const [result, setResult] = useState<BulkAssignResult | null>(null);
  const [run, busy, actionError] = useAction();

  useEffect(() => {
    const t = setInterval(list.reload, 20000);
    return () => clearInterval(t);
  }, [list.reload]);
  useRealtime((event) => {
    if (event === "conversation.updated" || event === "conversation.handoff" || event === "message.new") list.reload();
  });

  const items = list.data?.items ?? [];
  const set = (patch: Partial<Filters>) => {
    setF((prev) => ({ ...prev, ...patch }));
    setSelected(new Set());
  };
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChecked = items.length > 0 && items.every((i) => selected.has(i.id));

  async function assign() {
    if (!selected.size || (!target.agent_id && !target.group_id)) return;
    const body: Record<string, unknown> = { conversation_ids: [...selected] };
    if (target.agent_id) body.agent_id = Number(target.agent_id);
    if (target.group_id) body.group_id = Number(target.group_id);
    const r = await run(() => send<BulkAssignResult>("/api/monitoring/assign", "POST", body));
    if (r) {
      setResult(r);
      setSelected(new Set());
      list.reload();
    }
  }

  if (list.error?.includes("Solo supervisores")) {
    return (
      <>
        <PageHeader title="Monitoreo" />
        <Empty>El monitoreo es para supervisores y administradores.</Empty>
      </>
    );
  }

  const waiting = items.filter((i) => i.wait_minutes != null);
  const longest = Math.max(0, ...waiting.map((i) => i.wait_minutes ?? 0));
  return (
    <>
      <PageHeader
        title="Monitoreo"
        subtitle={
          meta.data?.scope === "groups"
            ? `Conversaciones de tus grupos: ${meta.data.groups.map((g) => g.name).join(", ") || "—"}`
            : "Todas las conversaciones de la empresa"
        }
      />
      <div className="stats">
        <Stat label="En la lista" value={fmtNum(items.length)} />
        <Stat label="Clientes esperando respuesta" value={fmtNum(waiting.length)} tone={waiting.length ? "warn" : "ok"} />
        <Stat label="Espera más larga" value={`${fmtNum(Math.round(longest))} min`} tone={longest >= 15 ? "bad" : longest >= 5 ? "warn" : "ok"} />
        <Stat label="Sin asesor" value={fmtNum(items.filter((i) => i.status === "human" && !i.assigned_agent).length)} />
      </div>

      <Card title="Filtros">
        <div className="inline" style={{ marginBottom: 10 }}>
          {PRESETS.map((p) => (
            <button key={p.label} className="small" onClick={() => setF({ ...EMPTY, ...p.f })}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="inline filters">
          <select value={f.status} onChange={(e) => set({ status: e.target.value })} aria-label="Estado">
            <option value="open">Abiertas</option>
            <option value="unassigned">En cola sin asesor</option>
            <option value="human">Con asesor</option>
            <option value="bot">Con el bot</option>
            <option value="closed">Cerradas</option>
            <option value="all">Todas</option>
          </select>
          <select value={f.group_id} onChange={(e) => set({ group_id: e.target.value })} aria-label="Grupo">
            <option value="">Todos los grupos</option>
            {meta.data?.groups.map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
          </select>
          <select value={f.agent_id} onChange={(e) => set({ agent_id: e.target.value })} aria-label="Asesor">
            <option value="">Todos los asesores</option>
            {agents.data?.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
          <select value={f.channel_id} onChange={(e) => set({ channel_id: e.target.value })} aria-label="Canal">
            <option value="">Todos los canales</option>
            {meta.data?.channels.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <input
            type="number" min={0} placeholder="Espera ≥ min" style={{ width: 130 }} value={f.waiting_min_gte}
            onChange={(e) => set({ waiting_min_gte: e.target.value })} aria-label="Esperando al menos (minutos)"
          />
          <input
            type="number" min={0} placeholder="En bot ≥ min" style={{ width: 130 }} value={f.stuck_in_bot_min}
            onChange={(e) => set({ stuck_in_bot_min: e.target.value })} aria-label="Estancada en el bot (minutos)"
          />
          <input
            placeholder="Etiqueta" style={{ width: 130 }} value={f.tag} onChange={(e) => set({ tag: e.target.value })}
            aria-label="Etiqueta"
          />
          <label className="inline small">
            <input type="checkbox" checked={f.unattended} onChange={(e) => set({ unattended: e.target.checked })} />
            Sin atender
          </label>
          <label className="inline small">
            <input type="checkbox" checked={f.sla_breached} onChange={(e) => set({ sla_breached: e.target.checked })} />
            SLA vencido
          </label>
          <select value={f.sort} onChange={(e) => set({ sort: e.target.value })} aria-label="Ordenar">
            <option value="wait">Mayor espera primero</option>
            <option value="last_message">Último mensaje</option>
            <option value="created">Más recientes</option>
          </select>
        </div>
      </Card>

      <Card
        title="Conversaciones"
        actions={
          <div className="inline">
            <span className="small muted">{selected.size} seleccionadas</span>
            <select
              value={target.group_id}
              onChange={(e) => setTarget({ agent_id: "", group_id: e.target.value })}
              aria-label="Asignar a grupo"
            >
              <option value="">Grupo…</option>
              {meta.data?.groups.map((g) => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
            <select
              value={target.agent_id}
              onChange={(e) => setTarget({ group_id: "", agent_id: e.target.value })}
              aria-label="Asignar a asesor"
            >
              <option value="">Asesor…</option>
              {agents.data?.filter((a) => a.is_active).map((a) => (
                <option key={a.id} value={a.id}>{a.name}{a.online ? " · conectado" : ""}</option>
              ))}
            </select>
            <button
              className="primary"
              disabled={busy || !selected.size || (!target.agent_id && !target.group_id)}
              onClick={assign}
            >
              Asignar
            </button>
          </div>
        }
      >
        <ErrorBox error={actionError ?? (list.error && !list.data ? list.error : null)} />
        {result && (
          <p className="small">
            {result.assigned.length} asignadas
            {result.skipped.length ? ` · ${result.skipped.length} omitidas (${result.skipped.map((s) => `#${s.id} ${s.reason}`).join(", ")})` : ""}
          </p>
        )}
        {!list.data ? (
          <Loading />
        ) : items.length === 0 ? (
          <Empty>No hay conversaciones con estos filtros</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox" aria-label="Seleccionar todas" checked={allChecked}
                      onChange={() => setSelected(allChecked ? new Set() : new Set(items.map((i) => i.id)))}
                    />
                  </th>
                  <th>Cliente</th>
                  <th>Estado</th>
                  <th>Grupo</th>
                  <th>Asesor</th>
                  <th className="num">Espera</th>
                  <th>Último mensaje</th>
                  <th>Transcripción</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c: MonitoringItem) => (
                  <tr key={c.id} className={selected.has(c.id) ? "selected" : undefined}>
                    <td>
                      <input type="checkbox" aria-label={`Seleccionar #${c.id}`} checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                    </td>
                    <td>
                      <Link href={`/conversaciones?c=${c.id}`} className="strong">
                        {CHANNEL_ICONS[c.channel_provider] ?? ""} {contactLabel(c.contact)}
                      </Link>
                      <div className="small muted">
                        #{c.id}
                        {c.is_returning ? " · recurrente" : ""}
                        {c.assignment_count > 1 ? ` · reasignada ×${c.assignment_count}` : ""}
                      </div>
                    </td>
                    <td>
                      <Badge tone={c.status === "human" ? "info" : c.status === "bot" ? "neutral" : "ok"}>
                        {STATUS_LABEL[c.status]}
                      </Badge>
                      {c.minutes_in_bot != null && c.minutes_in_bot >= 30 && (
                        <div className="small muted">en bot {fmtNum(Math.round(c.minutes_in_bot))} min</div>
                      )}
                    </td>
                    <td className="small">{c.group?.name ?? "—"}</td>
                    <td className="small">{c.assigned_agent?.name ?? <span className="muted">Sin asesor</span>}</td>
                    <td className="num">
                      {c.wait_minutes == null ? (
                        "—"
                      ) : (
                        <Badge tone={waitTone(c.wait_minutes)}>{fmtNum(Math.round(c.wait_minutes))} min</Badge>
                      )}
                    </td>
                    <td className="small">
                      <div className="muted">{timeAgo(c.last_message_at)}</div>
                      <div style={{ maxWidth: 260 }} className="preview">{c.last_message_preview ?? ""}</div>
                    </td>
                    <td className="small">
                      <a href={downloadUrl(`/api/conversations/${c.id}/transcript?format=pdf`)}>PDF</a>{" · "}
                      <a href={downloadUrl(`/api/conversations/${c.id}/transcript?format=txt`)}>TXT</a>{" · "}
                      <a href={downloadUrl(`/api/conversations/${c.id}/transcript?format=csv`)}>CSV</a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
