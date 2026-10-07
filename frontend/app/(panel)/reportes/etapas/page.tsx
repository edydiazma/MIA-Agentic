"use client";

import { STAGE_LABEL, fmtNum } from "@/lib/api";
import { Card, Stat } from "@/components/ui";
import ReportPage, { useReport } from "@/components/reports/ReportPage";
import { Funnel } from "@/components/reports/charts";
import type { StagesReport } from "@/components/reports/types";

export default function EtapasPage() {
  const { data, error, loading } = useReport<StagesReport>("/api/reports/stages");
  const v = (k: keyof StagesReport) => data?.[k] ?? 0;
  const total = v("lead") + v("prospect") + v("client") + v("lost");

  return (
    <ReportPage
      title="Etapas de contactos"
      subtitle="Cuántos contactos hay en cada etapa del embudo comercial (no incluye bloqueados). La etapa se cambia desde Clientes."
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Contactos" value={fmtNum(total)} />
            <Stat label="Clientes" value={fmtNum(v("client"))} tone="ok" />
            <Stat
              label="Conversión a cliente"
              value={total ? `${Math.round((100 * v("client")) / total)} %` : "—"}
              hint="Clientes sobre el total de contactos"
            />
            <Stat label="Perdidos" value={fmtNum(v("lost"))} />
          </div>
          <Card title="Embudo">
            <Funnel
              steps={[
                { label: STAGE_LABEL.lead, value: v("lead") + v("prospect") + v("client") },
                { label: STAGE_LABEL.prospect, value: v("prospect") + v("client") },
                { label: STAGE_LABEL.client, value: v("client") },
                { label: STAGE_LABEL.lost, value: v("lost"), muted: true },
              ]}
            />
            <p className="small muted" style={{ marginTop: 10 }}>
              Cada etapa incluye a los contactos que avanzaron más allá (un cliente también fue lead y prospecto). En
              etapa actual: {STAGE_LABEL.lead} {fmtNum(v("lead"))} · {STAGE_LABEL.prospect} {fmtNum(v("prospect"))} ·{" "}
              {STAGE_LABEL.client} {fmtNum(v("client"))}.
            </p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
