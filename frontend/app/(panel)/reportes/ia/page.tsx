"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { HEALTH_LABEL, type AIReport } from "@/lib/ai-types";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";

const PURPOSE: Record<string, string> = {
  chat: "Chat",
  classification: "Clasificación",
  learning: "Aprendizaje",
  flow: "Flujos",
  json_edit: "Edición JSON",
  qa: "Calidad (QA)",
  agent_test: "Pruebas de agentes",
  onboarding: "Onboarding",
  test: "Pruebas",
};
const usd = (n: number) => `US$ ${n.toLocaleString("es", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;

export default function AIReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<AIReport>("/api/reports/ai", range);
  const t = data?.totals;
  const rows = data?.by_connection ?? [];
  const chartData = (data?.series ?? []).map((r) => ({
    ...r,
    ok: Math.max(0, Number(r.calls ?? 0) - Number(r.errors ?? 0) - Number(r.fallbacks ?? 0)),
  }));

  return (
    <ReportPage
      title="IA y Cortex"
      subtitle={
        <>
          Salud, failover, latencia, tokens y costo de cada conexión a LLMs. Configúralas en{" "}
          <Link href="/automatizaciones/cortex/conexiones">Conexiones y failover</Link>.
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
            <Stat label="Llamadas" value={fmtNum(t.calls)} />
            <Stat label="Errores + lentas" value={fmtPct(t.error_pct)} tone={(t.error_pct ?? 0) > 5 ? "bad" : "ok"}
              hint={`${fmtNum(t.errors)} errores · ${fmtNum(t.slow)} lentas`} />
            <Stat label="Respondidas por failover" value={fmtNum(t.fallbacks)} hint="La primera conexión falló y respondió otra" />
            <Stat label="Tokens" value={fmtNum(t.input_tokens + t.output_tokens)} hint={`${fmtNum(t.input_tokens)} entrada · ${fmtNum(t.output_tokens)} salida`} />
            <Stat label="Costo estimado" value={usd(t.cost_usd)} hint="Según el costo por millón configurado en cada conexión" />
          </div>
          <Card title="Llamadas por día">
            {t.calls === 0 ? <Empty>Sin llamadas a LLMs en este periodo</Empty> : (
              <StackedDaily
                data={chartData}
                series={[
                  { key: "ok", label: "Correctas" },
                  { key: "fallbacks", label: "Por failover" },
                  { key: "errors", label: "Errores" },
                ]}
                title="Llamadas a LLMs por día"
              />
            )}
          </Card>
          <div className="grid2">
            <Card title="Llamadas por conexión">
              <BarList items={Object.values(rows.reduce<Record<string, { label: string; value: number }>>((acc, r) => {
                acc[r.name] = { label: r.name, value: (acc[r.name]?.value ?? 0) + r.calls };
                return acc;
              }, {}))} />
            </Card>
            <Card title="Costo por uso">
              <BarList format={usd} items={Object.values(rows.reduce<Record<string, { label: string; value: number }>>((acc, r) => {
                const k = PURPOSE[r.purpose] ?? r.purpose;
                acc[k] = { label: k, value: (acc[k]?.value ?? 0) + r.cost_usd };
                return acc;
              }, {}))} empty="Sin costo registrado (configura el costo por millón en las conexiones)" />
            </Card>
          </div>
          <Card title="Detalle por conexión y uso">
            {rows.length === 0 ? <Empty>Sin datos</Empty> : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Conexión</th><th>Uso</th><th>Estado</th><th className="num">Llamadas</th><th className="num">Error %</th>
                      <th className="num">Failover</th><th className="num">p50 / p95 (ms)</th><th className="num">Tokens</th><th className="num">Costo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={`${r.connection_id}-${r.purpose}`}>
                        <td><strong>{r.name}</strong>{r.model && <div className="small muted">{r.model}</div>}</td>
                        <td>{PURPOSE[r.purpose] ?? r.purpose}</td>
                        <td><Badge tone={r.state === "open" ? "bad" : r.state === "half_open" ? "warn" : "ok"}>{HEALTH_LABEL[r.state] ?? r.state}</Badge></td>
                        <td className="num">{fmtNum(r.calls)}</td>
                        <td className="num">{fmtPct(r.error_pct)}</td>
                        <td className="num">{fmtNum(r.fallbacks)}</td>
                        <td className="num">{r.latency_p50_ms ?? "—"} / {r.latency_p95_ms ?? "—"}</td>
                        <td className="num">{fmtNum(r.input_tokens + r.output_tokens)}</td>
                        <td className="num">{usd(r.cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="small muted">Las latencias por rango muestran el peor día del periodo; el detalle exacto está en Conexiones y failover → Llamadas recientes.</p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
