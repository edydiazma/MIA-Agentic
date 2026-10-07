"use client";

import { fmtNum } from "@/lib/api";
import { Card, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, HourStrip, ShareBar } from "@/components/reports/charts";
import { MSG_TYPE_LABEL, pct, type InboundReport } from "@/components/reports/types";

export default function InboundPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<InboundReport>("/api/reports/inbound", range);

  return (
    <ReportPage
      title="Inbound · Resumen"
      subtitle="Lo que escriben los clientes: volumen, tipo de mensaje, horario y origen"
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Mensajes recibidos" value={fmtNum(data.messages)} />
            <Stat label="Conversaciones nuevas" value={fmtNum(data.conversations)} />
            <Stat
              label="Desde anuncios"
              value={fmtNum(data.by_source.ads)}
              hint={`${pct(data.by_source.ads, data.conversations) ?? 0} % de las nuevas`}
            />
          </div>
          <Card title="Mensajes por hora del día">
            <HourStrip values={data.by_hour} />
          </Card>
          <div className="grid2" style={{ marginTop: 16 }}>
            <Card title="Tipo de mensaje">
              <BarList
                items={Object.entries(data.by_type).map(([k, value]) => ({ label: MSG_TYPE_LABEL[k] ?? k, value }))}
              />
            </Card>
            <Card title="Origen de las conversaciones nuevas">
              <ShareBar
                parts={[
                  { label: "Orgánico", value: data.by_source.organic },
                  { label: "Anuncios (Click to WA)", value: data.by_source.ads },
                ]}
              />
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
