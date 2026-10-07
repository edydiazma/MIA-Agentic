"use client";

import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList } from "@/components/reports/charts";
import type { InboundReport } from "@/components/reports/types";

export default function BotsPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<InboundReport>("/api/reports/inbound", range);
  const b = data?.bots;

  return (
    <ReportPage
      title="Inbound · Bots"
      subtitle="Desempeño del agente de IA: cuánto atiende y por qué transfiere a asesores"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {b && (
        <>
          <div className="stats">
            <Stat label="Mensajes del bot" value={fmtNum(b.messages)} />
            <Stat label="Conversaciones atendidas" value={fmtNum(b.conversations)} />
            <Stat label="Transferencias" value={fmtNum(b.handoffs)} />
            <Stat
              label="Tasa de transferencia"
              value={fmtPct(b.handoff_pct)}
              hint="Conversaciones del bot que pasaron a un asesor"
            />
          </div>
          <Card title="Motivos de transferencia más frecuentes">
            {b.top_handoff_reasons.length === 0 ? (
              <Empty>Sin transferencias en este periodo</Empty>
            ) : (
              <BarList items={b.top_handoff_reasons.map(([label, value]) => ({ label, value }))} />
            )}
          </Card>
        </>
      )}
    </ReportPage>
  );
}
