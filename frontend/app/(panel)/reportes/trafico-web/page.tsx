"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, Funnel, StackedDaily } from "@/components/reports/charts";
import { planError } from "@/components/attribution/common";
import type { WebTrafficReport } from "@/lib/attribution-types";

export default function TraficoWebPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<WebTrafficReport>("/api/reports/web-traffic", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Tráfico web"
      subtitle="Visitas del sitio, clics al botón de WhatsApp y conversaciones que generan, por fuente y campaña"
      range={range}
      onRange={setRange}
      loading={loading}
      error={planError(error)}
      ready={!!data}
      actions={<Link href="/configuraciones/atribucion">Configurar script</Link>}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Visitas" value={fmtNum(t.sessions)} hint={`${fmtNum(t.page_views)} páginas vistas`} />
            <Stat label="Clics a WhatsApp" value={fmtNum(t.wa_clicks)} hint={`${fmtPct(t.click_rate_pct)} de las visitas`} />
            <Stat
              label="Conversaciones"
              value={fmtNum(t.conversations)}
              hint={`${fmtPct(t.conversation_rate_pct)} de los clics`}
              tone={t.conversations ? "ok" : undefined}
            />
            <Stat label="Visitas pagadas" value={fmtNum(t.paid_sessions)} hint="Con gclid, fbclid o medio pagado" />
          </div>
          {t.sessions === 0 ? (
            <Card>
              <Empty>
                Sin visitas registradas en este periodo. Revisa que el script esté instalado en{" "}
                <Link href="/configuraciones/atribucion">Configuraciones › Atribución</Link>.
              </Empty>
            </Card>
          ) : (
            <>
              <div className="grid2">
                <Card title="Embudo">
                  <Funnel
                    steps={[
                      { label: "Visitas", value: t.sessions },
                      { label: "Clics a WhatsApp", value: t.wa_clicks },
                      { label: "Conversaciones", value: t.conversations },
                    ]}
                  />
                </Card>
                <Card title="Páginas de entrada">
                  <BarList items={data.top_landings.map((l) => ({ label: l.path, value: l.sessions }))} />
                </Card>
              </div>
              <Card>
                <StackedDaily
                  title="Por día"
                  data={data.series}
                  series={[
                    { key: "sessions", label: "Visitas" },
                    { key: "clicks", label: "Clics a WA" },
                    { key: "conversations", label: "Conversaciones" },
                  ]}
                />
              </Card>
              <Card title="Por fuente / medio / campaña">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Fuente</th>
                        <th>Medio</th>
                        <th>Campaña</th>
                        <th className="num">Visitas</th>
                        <th className="num">Clics a WA</th>
                        <th className="num">Tasa de clic</th>
                        <th className="num">Conversaciones</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_source.map((r) => (
                        <tr key={`${r.source}|${r.medium}|${r.campaign}`}>
                          <td className="strong">{r.source}</td>
                          <td>{r.medium}</td>
                          <td>{r.campaign || <span className="muted">—</span>}</td>
                          <td className="num">{fmtNum(r.sessions)}</td>
                          <td className="num">{fmtNum(r.clicks)}</td>
                          <td className="num">{fmtPct(r.click_rate_pct)}</td>
                          <td className="num">{fmtNum(r.conversations)}</td>
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
