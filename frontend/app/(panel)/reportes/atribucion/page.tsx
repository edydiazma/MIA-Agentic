"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, ShareBar } from "@/components/reports/charts";
import { fmtMoney, planError } from "@/components/attribution/common";
import { channelLabel, type AttributionReport } from "@/lib/attribution-types";

export default function AtribucionReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<AttributionReport>("/api/reports/attribution", range);
  const t = data?.totals;
  const up = data?.uploads;

  // ShareBar admite ≤ 4 partes: los 3 canales principales + «Otros».
  const share = (() => {
    const rows = data?.by_channel ?? [];
    const top = rows.slice(0, 3).map((r) => ({ label: r.label, value: r.conversations }));
    const rest = rows.slice(3).reduce((a, r) => a + r.conversations, 0);
    return rest ? [...top, { label: "Otros", value: rest }] : top;
  })();

  return (
    <ReportPage
      title="Atribución"
      subtitle="De dónde vienen las conversaciones (anuncios, web, campañas, directo) y cuáles terminan en venta"
      range={range}
      onRange={setRange}
      loading={loading}
      error={planError(error)}
      ready={!!data}
      actions={<Link href="/configuraciones/conversiones">Conversiones</Link>}
    >
      {data && t && up && (
        <>
          <div className="stats">
            <Stat label="Conversaciones nuevas" value={fmtNum(t.conversations)} />
            <Stat label="Con origen identificado" value={fmtPct(t.attributed_pct)} hint="Distinto de «Directo»" />
            <Stat label="Ventas" value={fmtNum(t.sales)} tone={t.sales ? "ok" : undefined} />
            <Stat label="Conversiones" value={fmtNum(t.conversions)} hint={fmtMoney(t.value)} />
            <Stat
              label="Envíos a plataformas"
              value={fmtNum(up.sent)}
              hint={`${fmtNum(up.pending)} pendientes · ${fmtNum(up.failed)} fallidos · ${fmtNum(up.skipped)} omitidos`}
              tone={up.failed ? "warn" : undefined}
            />
          </div>
          {t.conversations === 0 ? (
            <Card>
              <Empty>Sin conversaciones nuevas en este periodo.</Empty>
            </Card>
          ) : (
            <>
              <div className="grid2">
                <Card title="Participación por canal">
                  <ShareBar parts={share} />
                </Card>
                <Card title="Ventas por canal">
                  <BarList
                    items={data.by_channel.map((r) => ({ label: r.label, value: r.sales }))}
                    empty="Sin ventas en este periodo"
                  />
                </Card>
              </div>
              <Card title="Por canal">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Canal</th>
                        <th className="num">Conversaciones</th>
                        <th className="num">Cerradas</th>
                        <th className="num">Ventas</th>
                        <th className="num">Tasa de venta</th>
                        <th className="num">Conversiones</th>
                        <th className="num">Valor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_channel.map((r) => (
                        <tr key={r.channel}>
                          <td className="strong">{r.label}</td>
                          <td className="num">{fmtNum(r.conversations)}</td>
                          <td className="num">{fmtNum(r.closed)}</td>
                          <td className="num">{fmtNum(r.sales)}</td>
                          <td className="num">{fmtPct(r.sale_rate_pct)}</td>
                          <td className="num">{fmtNum(r.conversions)}</td>
                          <td className="num">{fmtMoney(r.value)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
              <Card title="Por campaña o anuncio">
                {data.by_campaign.length === 0 ? (
                  <Empty>Sin datos</Empty>
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Campaña / anuncio</th>
                          <th>Canal</th>
                          <th className="num">Conversaciones</th>
                          <th className="num">Ventas</th>
                          <th className="num">Tasa de venta</th>
                          <th className="num">Valor</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.by_campaign.map((r) => (
                          <tr key={`${r.channel}|${r.campaign}`}>
                            <td className="strong">{r.campaign}</td>
                            <td>{channelLabel(r.channel)}</td>
                            <td className="num">{fmtNum(r.conversations)}</td>
                            <td className="num">{fmtNum(r.sales)}</td>
                            <td className="num">{fmtPct(r.sale_rate_pct)}</td>
                            <td className="num">{fmtMoney(r.value)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </>
          )}
        </>
      )}
    </ReportPage>
  );
}
