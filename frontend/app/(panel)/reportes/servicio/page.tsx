"use client";

import { useState } from "react";
import { type AgentDetail, type Group, fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat, useApi } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { StackedDaily } from "@/components/reports/charts";
import { type ServiceKpis, type ServiceReport, fmtDuration } from "@/lib/supervision-types";

function KpiRow({ t }: { t: ServiceKpis }) {
  return (
    <>
      <div className="stats">
        <Stat label="Casos" value={fmtNum(t.cases)} hint={`${fmtNum(t.unique_contacts)} clientes únicos · ${fmtNum(t.returning_cases)} recurrentes`} />
        <Stat label="AHT (tiempo de atención)" value={fmtDuration(t.aht_s)} hint="De la asignación al cierre" />
        <Stat label="ASA (espera)" value={fmtDuration(t.asa_s)} hint="De la transferencia a la primera respuesta" />
        <Stat
          label="Tasa de atención"
          value={fmtPct(t.attention_rate_pct)}
          tone={t.attention_rate_pct == null ? undefined : t.attention_rate_pct >= 90 ? "ok" : t.attention_rate_pct >= 75 ? "warn" : "bad"}
          hint={`${fmtNum(t.attended)} de ${fmtNum(t.handoffs)} transferidas`}
        />
      </div>
      <div className="stats">
        <Stat
          label="Abandono"
          value={fmtPct(t.abandonment_rate_pct)}
          tone={t.abandonment_rate_pct == null ? undefined : t.abandonment_rate_pct <= 5 ? "ok" : t.abandonment_rate_pct <= 15 ? "warn" : "bad"}
          hint={`${fmtNum(t.abandoned)} se fueron en cola`}
        />
        <Stat label="No atendidas" value={fmtNum(t.not_attended)} hint="Transferidas y cerradas sin respuesta de asesor" />
        <Stat label="Contención del bot" value={fmtPct(t.bot_containment_pct)} hint={`${fmtNum(t.bot_only)} resueltas solo por el bot`} />
        <Stat label="Reasignadas" value={fmtNum(t.reassigned)} />
      </div>
    </>
  );
}

function Table<T extends ServiceKpis & { name: string }>({ rows, label }: { rows: T[]; label: string }) {
  if (!rows.length) return <Empty>Sin datos en este periodo</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>{label}</th>
            <th className="num">Casos</th>
            <th className="num">Clientes</th>
            <th className="num">Transferidas</th>
            <th className="num">Atención</th>
            <th className="num">Abandono</th>
            <th className="num">AHT</th>
            <th className="num">ASA</th>
            <th className="num">Cerradas</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name}>
              <td className="strong">{r.name}</td>
              <td className="num">{fmtNum(r.cases)}</td>
              <td className="num">{fmtNum(r.unique_contacts)}</td>
              <td className="num">{fmtNum(r.handoffs)}</td>
              <td className="num">{fmtPct(r.attention_rate_pct)}</td>
              <td className="num">{fmtPct(r.abandonment_rate_pct)}</td>
              <td className="num">{fmtDuration(r.aht_s)}</td>
              <td className="num">{fmtDuration(r.asa_s)}</td>
              <td className="num">{fmtNum(r.closed)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ServicioPage() {
  const [range, setRange] = useDateRange();
  const [groupId, setGroupId] = useState("");
  const [agentId, setAgentId] = useState("");
  const filtered = { ...range, ...(groupId ? { group_id: groupId } : {}), ...(agentId ? { agent_id: agentId } : {}) };
  const { data, error, loading } = useReport<ServiceReport>("/api/reports/service", filtered);
  const groups = useApi<Group[]>("/api/groups");
  const agents = useApi<AgentDetail[]>("/api/agents");

  return (
    <ReportPage
      title="Nivel de servicio"
      subtitle="AHT, espera, atención, abandono y contención del bot por grupo y asesor."
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
      actions={
        <div className="inline filters">
          <select value={groupId} onChange={(e) => setGroupId(e.target.value)} aria-label="Grupo">
            <option value="">Todos los grupos</option>
            {groups.data?.map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
          </select>
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)} aria-label="Asesor">
            <option value="">Todos los asesores</option>
            {agents.data?.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </div>
      }
    >
      {data && (
        <>
          <KpiRow t={data.totals} />
          <Card title="Casos por día">
            {data.totals.cases === 0 ? (
              <Empty>Sin conversaciones en este periodo</Empty>
            ) : (
              <StackedDaily
                data={data.series.map((d) => ({
                  day: d.day,
                  attended: d.attended,
                  not_attended: d.not_attended,
                  bot_only: d.bot_only,
                  other: Math.max(0, d.cases - d.attended - d.not_attended - d.bot_only),
                }))}
                series={[
                  { key: "attended", label: "Atendidas" },
                  { key: "not_attended", label: "No atendidas" },
                  { key: "bot_only", label: "Solo bot" },
                  { key: "other", label: "En curso" },
                ]}
                title="Casos por día"
              />
            )}
          </Card>
          <Card title="Por grupo">
            <Table rows={data.by_group} label="Grupo" />
          </Card>
          <Card title="Por asesor">
            <Table rows={data.by_agent} label="Asesor" />
          </Card>
        </>
      )}
    </ReportPage>
  );
}
