"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { Funnel, StackedDaily } from "@/components/reports/charts";
import { fmtMoney, type ProductsReport } from "@/lib/customer-types";

export default function ProductsReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<ProductsReport>("/api/reports/products", range);
  const t = data?.totals;
  const hasData = !!t && t.mentioned + t.interested + t.quoted + t.purchased > 0;

  return (
    <ReportPage
      title="Productos"
      subtitle={
        <>
          Demanda por producto en las conversaciones: menciones, interés, cotizaciones y compras, detectadas por la IA o
          registradas por los asesores, flujos, la API y los pedidos de WhatsApp. Catálogo en{" "}
          <Link href="/automatizaciones/catalogo">Catálogo de productos</Link>.
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
            <Stat label="Interesados" value={fmtNum(t.interested)} hint={`${fmtNum(t.mentioned)} menciones`} />
            <Stat label="Cotizados" value={fmtNum(t.quoted)} />
            <Stat label="Comprados" value={fmtNum(t.purchased)} />
            <Stat label="Valor comprado" value={fmtMoney(t.purchased_value)} hint="Cantidad × precio unitario" />
            <Stat label="Clientes" value={fmtNum(t.contacts)} hint="Con algún producto asociado" />
          </div>

          {!hasData ? (
            <Card>
              <Empty>Aún no hay productos asociados a conversaciones en este periodo.</Empty>
            </Card>
          ) : (
            <>
              <div className="grid2">
                <Card title="Embudo">
                  <Funnel
                    steps={[
                      { label: "Interés", value: t.interested + t.quoted + t.purchased },
                      { label: "Cotización", value: t.quoted + t.purchased },
                      { label: "Compra", value: t.purchased },
                    ]}
                  />
                </Card>
                <Card title="Por día">
                  <StackedDaily
                    data={data.series}
                    series={[
                      { key: "interested", label: "Interés" },
                      { key: "quoted", label: "Cotización" },
                      { key: "purchased", label: "Compra" },
                    ]}
                    title="Productos por etapa y día"
                  />
                </Card>
              </div>

              <Card title="Productos con más demanda">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Producto</th>
                        <th className="num">Menciones</th>
                        <th className="num">Interés</th>
                        <th className="num">Cotización</th>
                        <th className="num">Compra</th>
                        <th className="num">Conversión</th>
                        <th className="num">Valor</th>
                        <th className="num">Clientes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.top.map((r) => (
                        <tr key={r.product_key}>
                          <td>
                            <span className="strong">{r.name}</span>
                            {!r.product_id && <span className="muted small"> · fuera del catálogo</span>}
                          </td>
                          <td className="num">{fmtNum(r.mentioned)}</td>
                          <td className="num">{fmtNum(r.interested)}</td>
                          <td className="num">{fmtNum(r.quoted)}</td>
                          <td className="num">{fmtNum(r.purchased)}</td>
                          <td className="num">{fmtPct(r.conversion_pct)}</td>
                          <td className="num">{fmtMoney(r.purchased_value)}</td>
                          <td className="num">{fmtNum(r.contacts)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </>
          )}
        </>
      )}
    </ReportPage>
  );
}
