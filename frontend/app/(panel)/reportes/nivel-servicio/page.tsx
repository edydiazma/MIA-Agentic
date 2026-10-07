"use client";

import { fmtNum, fmtPct } from "@/lib/api";
import { Badge, Card, Empty, Stat, useApi } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import type { AgentsReport } from "@/components/reports/types";

export default function NivelServicioPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<AgentsReport>("/api/reports/agents", range);
  const target = useApi<{ sla_target_pct: number }>("/api/settings/reports").data?.sla_target_pct ?? 80;
  const meets = (v: number | null) => v != null && v >= target;
  const rows = (data?.agents ?? []).filter((a) => a.is_active || a.messages_sent || a.conversations_closed);

  return (
    <ReportPage
      title="Nivel de servicio"
      subtitle={
        data
          ? `Porcentaje de transferencias respondidas por un asesor en ${data.sla_minutes} min o menos. Meta: ${target} %.`
          : undefined
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
            <Stat
              label="Nivel de servicio"
              value={fmtPct(data.sla_pct)}
              tone={data.sla_pct == null ? undefined : meets(data.sla_pct) ? "ok" : "bad"}
              hint={
                data.sla_pct == null
                  ? "Sin transferencias respondidas"
                  : meets(data.sla_pct)
                    ? `✓ Cumple la meta de ${target} %`
                    : `✕ Por debajo de la meta de ${target} %`
              }
            />
            <Stat label="Transferencias" value={fmtNum(data.handoffs)} />
            <Stat
              label="Respondidas por un asesor"
              value={fmtNum(data.handoffs_answered)}
              hint={`${fmtNum(data.handoffs - data.handoffs_answered)} sin respuesta todavía`}
            />
          </div>
          <Card title="Por asesor">
            {rows.length === 0 ? (
              <Empty>Sin actividad de asesores en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Asesor</th>
                      <th className="num">Mensajes enviados</th>
                      <th className="num">Conversaciones cerradas</th>
                      <th className="num">Primera respuesta (mediana)</th>
                      <th className="num">Nivel de servicio</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((a) => (
                      <tr key={a.id}>
                        <td className="strong">{a.name}</td>
                        <td className="num">{fmtNum(a.messages_sent)}</td>
                        <td className="num">{fmtNum(a.conversations_closed)}</td>
                        <td className="num">
                          {a.first_response_median_min == null ? "—" : `${fmtNum(a.first_response_median_min)} min`}
                        </td>
                        <td className="num">
                          {a.sla_pct == null ? (
                            "—"
                          ) : (
                            <Badge tone={meets(a.sla_pct) ? "ok" : "bad"}>
                              {meets(a.sla_pct) ? "✓" : "✕"} {fmtPct(a.sla_pct)}
                            </Badge>
                          )}
                        </td>
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
