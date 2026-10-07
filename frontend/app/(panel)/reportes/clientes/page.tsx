"use client";

import Link from "next/link";
import { CHANNEL_LABELS, fmtNum } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";
import { channelLabel, fmtDays, type CustomersReport } from "@/lib/customer-types";

export default function CustomersReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<CustomersReport>("/api/reports/customers", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Clientes (BI)"
      subtitle={
        <>
          Altas, actividad, recencia y ciclo de vida de los clientes según su primera y última interacción. El detalle
          por cliente está en <Link href="/clientes">Clientes</Link> (columnas de interacción y exportación CSV).
        </>
      }
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Clientes" value={fmtNum(t.contacts)} hint="Total en la base" />
            <Stat label="Nuevos" value={fmtNum(t.new)} hint="Primera interacción en el periodo" />
            <Stat label="Activos" value={fmtNum(t.active)} hint="Con alguna interacción en el periodo" />
            <Stat label="Recurrentes" value={fmtNum(t.returning)} hint="Activos que ya habían escrito antes" />
            <Stat label="Vida promedio" value={fmtDays(t.avg_lifetime_days)} hint="Entre la primera y la última interacción" />
            <Stat
              label="Días entre conversaciones"
              value={fmtDays(t.avg_days_between_conversations)}
              hint="Promedio en clientes con más de una conversación"
            />
            <Stat
              label="Mediana sin interacción"
              value={fmtDays(t.median_days_since_last_interaction)}
              hint="Días desde la última interacción"
            />
          </div>

          <Card title="Nuevos y activos por día">
            {data.new_series.every((d) => !d.new && !d.active) ? (
              <Empty>Sin interacciones en este periodo</Empty>
            ) : (
              <StackedDaily
                data={data.new_series.map((d) => ({
                  day: d.day,
                  new: d.new,
                  returning_active: Math.max(0, d.active - d.new),
                }))}
                series={[
                  { key: "new", label: "Nuevos" },
                  { key: "returning_active", label: "Activos que ya eran clientes" },
                ]}
                title="Clientes nuevos y activos por día"
              />
            )}
          </Card>

          <div className="grid2">
            <Card title="Recencia (días sin interacción)">
              <BarList items={data.recency_buckets.map((b) => ({ label: b.label, value: b.count }))} />
            </Card>
            <Card title="Ciclo de vida (días entre primera y última interacción)">
              <BarList items={data.lifetime_buckets.map((b) => ({ label: b.label, value: b.count }))} />
            </Card>
          </div>

          <div className="grid2">
            <Card title="Por canal">
              {data.by_channel.length === 0 ? (
                <Empty>Sin datos</Empty>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Canal</th>
                        <th className="num">Clientes</th>
                        <th className="num">Nuevos</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_channel.map((r) => (
                        <tr key={r.provider}>
                          <td title={CHANNEL_LABELS[r.provider] ?? r.provider}>{channelLabel(r.provider)}</td>
                          <td className="num">{fmtNum(r.contacts)}</td>
                          <td className="num">{fmtNum(r.new)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Card title="Por último agente">
              <BarList
                items={data.by_agent.map((r) => ({ label: r.name ?? "Sin agente (solo bot)", value: r.contacts }))}
                empty="Sin clientes atendidos por asesores en este periodo"
              />
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
