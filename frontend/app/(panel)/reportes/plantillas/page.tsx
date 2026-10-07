"use client";

import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { pct, type OutboundReport } from "@/components/reports/types";

export default function PlantillasReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<OutboundReport>("/api/reports/outbound", range);
  const rows = (data?.individual_templates ?? []).map((r) => {
    const n = (k: string) => Number(r[k] ?? 0);
    return {
      template: r.template,
      total: n("total"),
      // Contadores acumulativos: un leído también fue entregado y enviado.
      sent: n("sent") + n("delivered") + n("read"),
      delivered: n("delivered") + n("read"),
      read: n("read"),
      failed: n("failed"),
    };
  });
  const total = rows.reduce((a, r) => a + r.total, 0);
  const read = rows.reduce((a, r) => a + r.read, 0);

  return (
    <ReportPage
      title="Outbound · Plantillas individuales"
      subtitle="Plantillas enviadas una a una por los asesores (por ejemplo, para escribir fuera de la ventana de 24 h)"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Plantillas enviadas" value={fmtNum(total)} />
            <Stat label="Plantillas distintas" value={fmtNum(rows.length)} />
            <Stat label="Tasa de lectura" value={fmtPct(pct(read, total))} />
          </div>
          <Card title="Por plantilla">
            {rows.length === 0 ? (
              <Empty>No se enviaron plantillas individuales en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Plantilla</th>
                      <th className="num">Total</th>
                      <th className="num">Enviados</th>
                      <th className="num">Entregados</th>
                      <th className="num">Leídos</th>
                      <th className="num">Fallidos</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.template}>
                        <td className="strong">{r.template}</td>
                        <td className="num">{fmtNum(r.total)}</td>
                        <td className="num">{fmtNum(r.sent)}</td>
                        <td className="num">{fmtNum(r.delivered)}</td>
                        <td className="num">
                          {fmtNum(r.read)} <small className="muted">{fmtPct(pct(r.read, r.total))}</small>
                        </td>
                        <td className="num">{fmtNum(r.failed)}</td>
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
