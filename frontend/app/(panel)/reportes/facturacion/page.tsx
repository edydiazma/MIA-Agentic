"use client";

import { fmtNum } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { StackedDaily } from "@/components/reports/charts";
import { PRICING_LABEL, type BillingReport } from "@/components/reports/types";

/** Orden fijo de categorías para que cada una conserve su color. */
const ORDER = ["marketing", "utility", "authentication", "service"];

export default function FacturacionPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<BillingReport>("/api/reports/billing", range);
  const cats = Object.entries(data?.categories ?? {}).sort(
    (a, b) => (ORDER.indexOf(a[0]) + 1 || 99) - (ORDER.indexOf(b[0]) + 1 || 99),
  );
  // Máximo 4 series en el gráfico; el resto se agrupa en «Otras».
  const chartKeys = ORDER.filter((k) => data?.categories[k]?.billable);
  const extra = cats.map(([k]) => k).filter((k) => !ORDER.includes(k) && data?.categories[k]?.billable);
  const series = [
    ...chartKeys.map((k) => ({ key: k, label: PRICING_LABEL[k] ?? k })),
    ...(extra.length ? [{ key: "__other", label: "Otras" }] : []),
  ].slice(0, 4);
  const chartData = (data?.series ?? []).map((row) => ({
    ...row,
    __other: extra.reduce((a, k) => a + Number(row[k] ?? 0), 0),
  }));

  return (
    <ReportPage
      title="Facturación"
      subtitle="Mensajes cobrables según la información de precios que Meta envía en cada entrega"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Mensajes cobrables" value={fmtNum(data.billable_total)} />
            {ORDER.map((k) => (
              <Stat key={k} label={PRICING_LABEL[k]} value={fmtNum(data.categories[k]?.billable ?? 0)} />
            ))}
          </div>
          <Card title="Mensajes cobrables por día">
            {series.length === 0 ? (
              <Empty>Meta no reportó mensajes cobrables en este periodo</Empty>
            ) : (
              <StackedDaily data={chartData} series={series} title="Mensajes cobrables por día y categoría" />
            )}
          </Card>
          <Card title="Por categoría">
            {cats.length === 0 ? (
              <Empty>Sin información de precios en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Categoría</th>
                      <th className="num">Mensajes con precio</th>
                      <th className="num">Cobrables</th>
                      <th className="num">Sin costo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cats.map(([k, v]) => (
                      <tr key={k}>
                        <td>{PRICING_LABEL[k] ?? k}</td>
                        <td className="num">{fmtNum(v.total ?? 0)}</td>
                        <td className="num strong">{fmtNum(v.billable ?? 0)}</td>
                        <td className="num">{fmtNum((v.total ?? 0) - (v.billable ?? 0))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="small muted" style={{ marginTop: 10 }}>
              {data.note} Consulta las tarifas vigentes en Meta Business Suite → Facturación.
            </p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
