"use client";

import { fmtNum } from "@/lib/api";
import { Card, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { Funnel, ShareBar } from "@/components/reports/charts";
import { SENDER_LABEL, type OutboundReport } from "@/components/reports/types";

export default function OutboundPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<OutboundReport>("/api/reports/outbound", range);
  const st = data?.by_status ?? {};
  const n = (k: string) => st[k] ?? 0;
  // Un mensaje leído también fue entregado y enviado.
  const sent = n("sent") + n("delivered") + n("read");
  const delivered = n("delivered") + n("read");

  return (
    <ReportPage
      title="Outbound · Resumen"
      subtitle="Mensajes enviados por el bot, los asesores y las campañas"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Mensajes enviados" value={fmtNum(data.total)} />
            <Stat label="Entregados" value={fmtNum(delivered)} />
            <Stat label="Leídos" value={fmtNum(n("read"))} />
            <Stat label="Fallidos" value={fmtNum(n("failed"))} tone={n("failed") ? "bad" : undefined} />
          </div>
          <div className="grid2">
            <Card title="Quién envía">
              <ShareBar
                parts={["bot", "agent", "campaign"].map((k) => ({ label: SENDER_LABEL[k], value: data.by_sender[k] ?? 0 }))}
              />
            </Card>
            <Card title="Embudo de entrega">
              <Funnel
                steps={[
                  { label: "Enviados", value: sent },
                  { label: "Entregados", value: delivered },
                  { label: "Leídos", value: n("read") },
                  { label: "Fallidos", value: n("failed"), muted: true },
                ]}
              />
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
