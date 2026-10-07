"use client";

import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";
import type { GeneralReport } from "@/components/reports/types";

const SERIES = [
  { key: "inbound", label: "Entrantes" },
  { key: "bot", label: "Bot" },
  { key: "agent", label: "Asesor" },
  { key: "campaign", label: "Campañas" },
];

export default function GeneralPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<GeneralReport>("/api/reports/general", range);
  const t = data?.totals;

  return (
    <ReportPage
      title="Reporte general"
      subtitle="Volumen de conversaciones y mensajes, transferencias y tiempos de respuesta"
      range={range}
      onRange={setRange}
      csv
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Conversaciones nuevas" value={fmtNum(t.new_conversations)} />
            <Stat label="Mensajes entrantes" value={fmtNum(t.inbound_messages)} />
            <Stat label="Mensajes del bot" value={fmtNum(t.bot_messages)} />
            <Stat label="Mensajes de asesores" value={fmtNum(t.agent_messages)} />
            <Stat label="Mensajes de campañas" value={fmtNum(t.campaign_messages)} />
            <Stat label="Transferencias a asesor" value={fmtNum(t.handoffs)} />
            <Stat label="Conversaciones cerradas" value={fmtNum(t.closed)} />
            <Stat
              label="Resueltas solo por el bot"
              value={fmtPct(t.bot_resolution_pct)}
              hint="De las cerradas, sin intervención humana"
            />
            <Stat
              label="Primera respuesta (mediana)"
              value={t.first_response_median_min == null ? "—" : `${fmtNum(t.first_response_median_min)} min`}
              hint="Desde la transferencia"
            />
            <Stat label={`Dentro del SLA (${t.sla_minutes} min)`} value={fmtPct(t.sla_pct)} />
          </div>
          <Card title="Mensajes por día">
            <StackedDaily data={data.series} series={SERIES} title="Mensajes por día según el remitente" />
          </Card>
          <Card title="Cierres por tipificación">
            <BarList
              items={Object.entries(data.typifications).map(([label, value]) => ({ label, value }))}
              empty="No se cerraron conversaciones en este periodo"
            />
          </Card>
        </>
      )}
    </ReportPage>
  );
}
