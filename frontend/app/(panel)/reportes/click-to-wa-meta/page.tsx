"use client";

import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { pct, type CtwaReport } from "@/components/reports/types";
import s from "@/components/reports/viz.module.css";

export default function ClickToWaMetaPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<CtwaReport>("/api/reports/ctwa", range);
  const sales = data?.ads.reduce((a, r) => a + r.sales, 0) ?? 0;
  const handoffs = data?.ads.reduce((a, r) => a + r.handoffs, 0) ?? 0;

  return (
    <ReportPage
      title="Inbound · Click to WA Meta"
      subtitle="Conversaciones que llegaron desde anuncios de Facebook e Instagram"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      <div className={s.explain}>
        Meta envía los datos del anuncio (<em>referral</em>) en el primer mensaje de cada conversación que se inicia
        desde un anuncio Click to WhatsApp. Las ventas cuentan las conversaciones cerradas con la tipificación
        «Venta».
      </div>
      {data && (
        <>
          <div className="stats">
            <Stat label="Conversaciones desde anuncios" value={fmtNum(data.total)} />
            <Stat label="Transferidas a asesor" value={fmtNum(handoffs)} />
            <Stat label="Ventas" value={fmtNum(sales)} tone={sales ? "ok" : undefined} />
            <Stat label="Tasa de venta" value={fmtPct(pct(sales, data.total))} />
          </div>
          <Card title="Por anuncio">
            {data.ads.length === 0 ? (
              <Empty>No llegaron conversaciones desde anuncios en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Anuncio</th>
                      <th>Tipo</th>
                      <th className="num">Conversaciones</th>
                      <th className="num">Transferidas</th>
                      <th className="num">Ventas</th>
                      <th className="num">Tasa de venta</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.ads.map((ad) => (
                      <tr key={ad.ad_id ?? ad.headline ?? "?"}>
                        <td>
                          <div className="strong">
                            {ad.url ? (
                              <a href={ad.url} target="_blank" rel="noreferrer">
                                {ad.headline || "Sin título"}
                              </a>
                            ) : (
                              ad.headline || "Sin título"
                            )}
                          </div>
                          {ad.ad_id && <div className="small muted">ID {ad.ad_id}</div>}
                        </td>
                        <td>{ad.source_type === "ad" ? "Anuncio" : ad.source_type === "post" ? "Publicación" : ad.source_type ?? "—"}</td>
                        <td className="num">{fmtNum(ad.conversations)}</td>
                        <td className="num">{fmtNum(ad.handoffs)}</td>
                        <td className="num">{fmtNum(ad.sales)}</td>
                        <td className="num">{fmtPct(pct(ad.sales, ad.conversations))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </ReportPage>
  );
}
