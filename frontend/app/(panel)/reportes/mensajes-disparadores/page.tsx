"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";
import { fmtMoney, planError } from "@/components/attribution/common";
import { PLATFORM_LABEL, type WaLinksReport } from "@/lib/attribution-types";

export default function MensajesDisparadoresReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<WaLinksReport>("/api/reports/wa-links", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Mensajes disparadores"
      subtitle="Clics, conversaciones y conversiones de cada enlace de WhatsApp con tracking. Las conversaciones cuentan el último toque; los primeros toques y regresos muestran el recorrido completo."
      range={range}
      onRange={setRange}
      loading={loading}
      error={planError(error)}
      ready={!!data}
      actions={<Link href="/configuraciones/mensajes-disparadores">Configurar mensajes</Link>}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Clics en enlaces cortos" value={fmtNum(t.clicks)} />
            <Stat label="Conversaciones" value={fmtNum(t.conversations)} hint="Atribuidas al enlace (último toque)" />
            <Stat label="Primeros toques" value={fmtNum(t.first_touches)} hint={`${fmtNum(t.retouches)} regresos por otro enlace o anuncio`} />
            <Stat label="Conversiones" value={fmtNum(t.conversions)} hint={fmtMoney(t.conversion_value)} tone={t.conversions ? "ok" : undefined} />
          </div>

          {t.clicks + t.conversations + t.first_touches === 0 ? (
            <Card>
              <Empty>Ningún mensaje disparador tuvo actividad en este periodo.</Empty>
            </Card>
          ) : (
            <>
              <Card title="Por día">
                <StackedDaily
                  data={data.series}
                  series={[
                    { key: "conversations", label: "Conversaciones" },
                    { key: "retouches", label: "Regresos" },
                  ]}
                  title="Conversaciones de mensajes disparadores por día"
                />
              </Card>
              <div className="grid2">
                <Card title="Conversaciones por mensaje">
                  <BarList items={data.links.map((l) => ({ label: l.name, value: l.conversations }))} />
                </Card>
                <Card title="Valor de conversiones">
                  <BarList
                    format={(n) => fmtMoney(n)}
                    items={data.links.map((l) => ({ label: l.name, value: l.conversion_value }))}
                    empty="Sin conversiones con valor en este periodo"
                  />
                </Card>
              </div>
              <Card title="Detalle">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Mensaje</th>
                        <th>Plataforma</th>
                        <th className="num">Clics</th>
                        <th className="num">Conversaciones</th>
                        <th className="num">Clic → chat</th>
                        <th className="num">Primeros toques</th>
                        <th className="num">Regresos</th>
                        <th className="num">Conversiones</th>
                        <th className="num">Tasa</th>
                        <th className="num">Valor</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.links.map((l) => (
                        <tr key={l.link_id}>
                          <td>
                            <span className="strong">{l.name}</span> {!l.is_active && <Badge tone="warn">Pausado</Badge>}
                            {l.slug && <div className="small muted">/t/l/{l.slug}</div>}
                          </td>
                          <td>{l.platform ? PLATFORM_LABEL[l.platform] ?? l.platform : "—"}</td>
                          <td className="num">{fmtNum(l.clicks)}</td>
                          <td className="num">{fmtNum(l.conversations)}</td>
                          <td className="num">{fmtPct(l.click_to_chat_pct)}</td>
                          <td className="num">{fmtNum(l.first_touches)}</td>
                          <td className="num">{fmtNum(l.retouches)}</td>
                          <td className="num">{fmtNum(l.conversions)}</td>
                          <td className="num">{fmtPct(l.conversion_rate_pct)}</td>
                          <td className="num">{fmtMoney(l.conversion_value)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="small muted">
                  «Clic → chat» solo aplica a enlaces cortos; los anuncios Click to WhatsApp y el texto escrito sin enlace no registran
                  clics aquí.
                </p>
              </Card>
            </>
          )}
        </>
      )}
    </ReportPage>
  );
}
