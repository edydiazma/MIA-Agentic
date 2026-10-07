"use client";

import Link from "next/link";
import { fmtNum, fmtPct } from "@/lib/api";
import { Card, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, ShareBar, StackedDaily } from "@/components/reports/charts";
import { CALL_STATUS_LABEL, HANDLER_LABEL, fmtDuration, type CallHandler, type CallStats, type CallStatus } from "@/lib/voice-types";

const SERIES = [
  { key: "answered", label: "Atendidas" },
  { key: "unanswered", label: "No atendidas" },
];

export default function LlamadasReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<CallStats>("/api/calls/stats", range);
  const m = data?.minutes_month;
  const missed = (data?.by_status.missed ?? 0) + (data?.by_status.rejected ?? 0) + (data?.by_status.failed ?? 0);

  return (
    <ReportPage
      title="Llamadas"
      subtitle={
        <>
          Llamadas de WhatsApp atendidas por asesores y agentes de voz ·{" "}
          <Link href="/automatizaciones/llamadas">Ver historial</Link>
        </>
      }
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Llamadas recibidas" value={fmtNum(data.total)} />
            <Stat label="Atendidas" value={fmtNum(data.answered)} tone="ok" />
            <Stat label="Perdidas o rechazadas" value={fmtNum(missed)} tone={missed ? "bad" : undefined} />
            <Stat label="Tasa de atención" value={fmtPct(data.answer_rate)} />
            <Stat label="Duración promedio" value={fmtDuration(data.avg_duration_s)} hint="De las atendidas" />
            <Stat label="Minutos hablados" value={fmtNum(data.talk_minutes)} />
            {m && (
              <Stat
                label="Minutos de voz del mes"
                value={fmtNum(Math.round(Number(m.used)))}
                hint={m.limit == null ? "Plan sin límite" : `de ${fmtNum(Number(m.limit))} del plan`}
                tone={m.limit != null && Number(m.used) >= Number(m.limit) ? "bad" : undefined}
              />
            )}
          </div>
          <Card title="Llamadas por día">
            <StackedDaily
              data={data.daily.map((d) => ({ day: d.day, answered: d.answered, unanswered: d.calls - d.answered }))}
              series={SERIES}
              title="Llamadas por día, atendidas y no atendidas"
            />
          </Card>
          <div className="grid2" style={{ marginTop: 16 }}>
            <Card title="Quién atendió">
              <ShareBar
                parts={(Object.keys(HANDLER_LABEL) as CallHandler[]).map((k) => ({ label: HANDLER_LABEL[k], value: data.by_handler[k] ?? 0 }))}
              />
            </Card>
            <Card title="Resultado">
              <BarList
                items={(Object.keys(data.by_status) as CallStatus[]).map((k) => ({
                  label: CALL_STATUS_LABEL[k] ?? k,
                  value: data.by_status[k] ?? 0,
                }))}
                empty="No hubo llamadas en este periodo"
              />
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
