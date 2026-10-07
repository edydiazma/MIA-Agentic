"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { pct, type OutboundReport } from "@/components/reports/types";

const STATUS: Record<string, [string, "neutral" | "ok" | "warn" | "bad" | "info"]> = {
  draft: ["Borrador", "neutral"],
  running: ["Enviando", "info"],
  done: ["Enviada", "ok"],
  failed: ["Falló", "bad"],
};

export default function CampanasReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<OutboundReport>("/api/reports/outbound", range);
  const rows = data?.campaigns ?? [];
  const sum = (k: "total" | "sent" | "delivered" | "read" | "failed") => rows.reduce((a, r) => a + (r[k] ?? 0), 0);

  return (
    <ReportPage
      title="Outbound · Campañas de plantillas"
      subtitle="Resultado de los envíos masivos creados en el periodo"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Campañas" value={fmtNum(rows.length)} />
            <Stat label="Destinatarios" value={fmtNum(sum("total"))} />
            <Stat label="Tasa de entrega" value={fmtPct(pct(sum("delivered"), sum("total")))} />
            <Stat label="Tasa de lectura" value={fmtPct(pct(sum("read"), sum("total")))} />
            <Stat label="Tasa de fallo" value={fmtPct(pct(sum("failed"), sum("total")))} tone={sum("failed") ? "bad" : undefined} />
          </div>
          <Card title="Campañas">
            {rows.length === 0 ? (
              <Empty>No hay campañas en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Campaña</th>
                      <th>Plantilla</th>
                      <th>Estado</th>
                      <th className="num">Destinatarios</th>
                      <th className="num">Entregados</th>
                      <th className="num">Leídos</th>
                      <th className="num">Fallidos</th>
                      <th className="num">No entregados</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => {
                      const [label, tone] = STATUS[c.status] ?? [c.status, "neutral"];
                      return (
                        <tr key={c.id}>
                          <td className="strong">
                            <Link href={`/campanas?id=${c.id}`}>{c.name}</Link>
                          </td>
                          <td className="small">{c.template}</td>
                          <td>
                            <Badge tone={tone}>{label}</Badge>
                          </td>
                          <td className="num">{fmtNum(c.total ?? 0)}</td>
                          <td className="num">
                            {fmtNum(c.delivered ?? 0)} <small className="muted">{fmtPct(pct(c.delivered, c.total))}</small>
                          </td>
                          <td className="num">
                            {fmtNum(c.read ?? 0)} <small className="muted">{fmtPct(pct(c.read, c.total))}</small>
                          </td>
                          <td className="num">
                            {fmtNum(c.failed ?? 0)} <small className="muted">{fmtPct(pct(c.failed, c.total))}</small>
                          </td>
                          <td className="num">{fmtNum(c.not_delivered ?? 0)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <p className="small muted" style={{ marginTop: 10 }}>
              «No entregados»: mensajes aceptados por WhatsApp que el teléfono del cliente todavía no confirmó.
            </p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
