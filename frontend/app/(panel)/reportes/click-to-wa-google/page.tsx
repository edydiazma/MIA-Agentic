"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";
import { fmtMoney, planError } from "@/components/attribution/common";
import type { ClickToWaGoogleReport } from "@/lib/attribution-types";

type Row = { conversations: number; sales: number; conversions: number; value: number };

function Table({ rows, label, keyOf }: { rows: Row[]; label: string; keyOf: (r: Row) => string }) {
  if (!rows.length) return <Empty>Sin datos en este periodo</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>{label}</th>
            <th className="num">Conversaciones</th>
            <th className="num">Ventas</th>
            <th className="num">Tasa de venta</th>
            <th className="num">Conversiones</th>
            <th className="num">Valor</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={keyOf(r)}>
              <td className="strong">{keyOf(r)}</td>
              <td className="num">{fmtNum(r.conversations)}</td>
              <td className="num">{fmtNum(r.sales)}</td>
              <td className="num">{fmtPct(r.conversations ? (100 * r.sales) / r.conversations : null)}</td>
              <td className="num">{fmtNum(r.conversions)}</td>
              <td className="num">{fmtMoney(r.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ClickToWaGooglePage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<ClickToWaGoogleReport>("/api/reports/click-to-wa-google", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Click to WA Google"
      subtitle="Conversaciones que llegan desde anuncios de Google Ads (gclid) y conversiones devueltas a Google"
      range={range}
      onRange={setRange}
      loading={loading}
      error={planError(error)}
      ready={!!data}
      actions={<Link href="/configuraciones/conversiones">Conversiones</Link>}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Clics a WhatsApp desde Google" value={fmtNum(t.wa_clicks)} />
            <Stat label="Conversaciones" value={fmtNum(t.conversations)} />
            <Stat label="Ventas" value={fmtNum(t.sales)} hint={`${fmtPct(t.sale_rate_pct)} de las conversaciones`} tone={t.sales ? "ok" : undefined} />
            <Stat label="Valor convertido" value={fmtMoney(t.value)} />
            <Stat
              label="Subidas a Google Ads"
              value={fmtNum(t.uploads_sent)}
              hint={`${fmtNum(t.uploads_pending)} pendientes · ${fmtNum(t.uploads_failed)} fallidas`}
              tone={t.uploads_failed ? "warn" : undefined}
            />
          </div>
          {t.conversations === 0 ? (
            <Card>
              <Empty>
                Sin conversaciones desde Google Ads en este periodo. Instala el script en tu sitio (
                <Link href="/configuraciones/atribucion">Atribución</Link>) y activa el etiquetado automático (gclid) en Google Ads.
              </Empty>
            </Card>
          ) : (
            <>
              <Card>
                <StackedDaily
                  title="Conversaciones y ventas por día"
                  data={data.series.map((d) => ({ day: d.day, sales: d.sales, others: d.conversations - d.sales }))}
                  series={[
                    { key: "sales", label: "Con venta" },
                    { key: "others", label: "Sin venta" },
                  ]}
                />
              </Card>
              <Card title="Por campaña">
                <Table rows={data.by_campaign} label="Campaña" keyOf={(r) => (r as Row & { campaign: string }).campaign} />
              </Card>
              <div className="grid2">
                <Card title="Palabras clave con más conversaciones">
                  <BarList items={data.by_keyword.map((k) => ({ label: k.keyword, value: k.conversations }))} />
                </Card>
                <Card title="Palabras clave con ventas">
                  <BarList
                    items={data.by_keyword.map((k) => ({ label: k.keyword, value: k.sales }))}
                    empty="Ninguna palabra clave con ventas en este periodo"
                  />
                </Card>
              </div>
            </>
          )}
        </>
      )}
    </ReportPage>
  );
}
