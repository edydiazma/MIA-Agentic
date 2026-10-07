"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { KIND_LABELS, type CopilotReport } from "@/lib/copilot-types";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { StackedDaily } from "@/components/reports/charts";

const ms = (n: number | null) => (n == null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`);

export default function CopilotReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<CopilotReport>("/api/reports/copilot", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Copiloto"
      subtitle={
        <>
          Cuánto usan los asesores las sugerencias de la IA: mostradas, usadas tal cual, editadas y descartadas. Se
          configura en <Link href="/configuraciones/copiloto">Configuraciones → Copiloto</Link>.
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
            <Stat label="Sugerencias mostradas" value={fmtNum(t.shown)} />
            <Stat label="Adopción" value={fmtPct(t.adoption_pct)} hint="Usadas tal cual o editadas / mostradas"
              tone={t.adoption_pct == null ? undefined : t.adoption_pct >= 40 ? "ok" : t.adoption_pct >= 20 ? "warn" : "bad"} />
            <Stat label="Usadas sin cambios" value={fmtPct(t.accepted_unchanged_pct)} hint="De las usadas" />
            <Stat label="Descartadas" value={fmtNum(t.dismissed)} />
            <Stat label="Latencia promedio" value={ms(t.avg_latency_ms)} />
          </div>
          <Card title="Por día">
            {t.shown === 0 ? (
              <Empty>Sin sugerencias en el periodo</Empty>
            ) : (
              <StackedDaily
                title="Sugerencias del copiloto por día"
                data={data.series}
                series={[
                  { key: "used", label: "Usadas" },
                  { key: "dismissed", label: "Descartadas" },
                ]}
              />
            )}
          </Card>
          <div className="grid2">
            <Card title="Por asesor">
              {data.by_agent.length === 0 ? (
                <Empty>Sin datos</Empty>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Asesor</th>
                        <th className="num">Mostradas</th>
                        <th className="num">Adopción</th>
                        <th className="num">Sin cambios</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_agent.map((a) => (
                        <tr key={a.agent_id}>
                          <td>{a.name ?? "Sin asesor"}</td>
                          <td className="num">{fmtNum(a.shown)}</td>
                          <td className="num">{fmtPct(a.adoption_pct)}</td>
                          <td className="num">{fmtPct(a.accepted_unchanged_pct)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Card title="Por función">
              {data.by_kind.length === 0 ? (
                <Empty>Sin datos</Empty>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Función</th>
                        <th className="num">Generadas</th>
                        <th className="num">Adopción</th>
                        <th className="num">Latencia</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_kind.map((k) => (
                        <tr key={k.kind}>
                          <td>{KIND_LABELS[k.kind] ?? k.kind}</td>
                          <td className="num">{fmtNum(k.shown)}</td>
                          <td className="num">{fmtPct(k.adoption_pct)}</td>
                          <td className="num">{ms(k.avg_latency_ms)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
