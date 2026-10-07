"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { ShareBar, StackedDaily } from "@/components/reports/charts";
import { fmtScore, scoreTone, type QAReport } from "@/lib/quality-types";

export default function QualityReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<QAReport>("/api/reports/qa", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Calidad y coaching"
      subtitle={
        <>
          Puntaje de calidad de asesores y bot según las rúbricas de{" "}
          <Link href="/automatizaciones/calidad">Automatizaciones › Calidad (QA)</Link>, fallas críticas y sentimiento del cliente.
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
            <Stat label="Conversaciones revisadas" value={fmtNum(t.reviews)} />
            <Stat label="Puntaje promedio" value={fmtScore(t.avg_score)}
              tone={t.avg_score == null ? undefined : t.avg_score >= 80 ? "ok" : t.avg_score >= 60 ? "warn" : "bad"} />
            <Stat label="Fallas críticas" value={fmtPct(t.critical_pct)} tone={t.critical_failed ? "bad" : undefined}
              hint={`${fmtNum(t.critical_failed)} conversaciones`} />
            <Stat label="Clientes insatisfechos" value={fmtPct(t.negative_pct)} hint={`${fmtNum(t.negative)} con sentimiento negativo`} />
            <Stat label="Coaching pendiente" value={fmtNum(t.open_coaching)} hint={`${fmtNum(t.disputed)} revisiones impugnadas`} />
          </div>

          <Card title="Revisiones por día y sentimiento">
            {t.reviews === 0 ? <Empty>Sin revisiones en este periodo</Empty> : (
              <StackedDaily
                data={data.series.map((d) => ({ day: d.day, positive: d.positive, neutral: d.neutral, negative: d.negative }))}
                series={[
                  { key: "positive", label: "Positivo" },
                  { key: "neutral", label: "Neutral o mixto" },
                  { key: "negative", label: "Negativo" },
                ]}
                title="Revisiones por día según el sentimiento del cliente"
              />
            )}
          </Card>

          <div className="grid2">
            <Card title="Asesores (de menor a mayor puntaje)">
              {data.agents.length === 0 ? <Empty>Sin revisiones de asesores</Empty> : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Asesor</th>
                        <th className="num">Revisiones</th>
                        <th className="num">Puntaje</th>
                        <th className="num">Críticas</th>
                        <th className="num">Negativas</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.agents.map((a) => (
                        <tr key={a.agent_id}>
                          <td className="strong">{a.name}</td>
                          <td className="num">{fmtNum(a.reviews)}</td>
                          <td className="num"><Badge tone={scoreTone(a.avg_score)}>{fmtScore(a.avg_score)}</Badge></td>
                          <td className="num">{a.critical_failed ? <Badge tone="bad">{fmtNum(a.critical_failed)}</Badge> : "—"}</td>
                          <td className="num">{fmtPct(a.negative_pct)}</td>
                          <td><Link className="small" href={`/coaching?agent=${a.agent_id}`}>Coaching</Link></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Card title="Bot">
              {data.bot.reviews === 0 ? <Empty>Sin revisiones del bot (ajusta la muestra de la rúbrica «Calidad del bot»)</Empty> : (
                <div style={{ display: "grid", gap: 12 }}>
                  <div className="stats">
                    <Stat label="Revisiones" value={fmtNum(data.bot.reviews)} />
                    <Stat label="Puntaje" value={fmtScore(data.bot.avg_score)} />
                    <Stat label="Inventó datos / críticas" value={fmtNum(data.bot.critical_failed)}
                      tone={data.bot.critical_failed ? "bad" : undefined} />
                  </div>
                  <ShareBar parts={[
                    { label: "Positivo", value: data.bot.positive },
                    { label: "Neutral o mixto", value: data.bot.neutral },
                    { label: "Negativo", value: data.bot.negative },
                  ]} />
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
