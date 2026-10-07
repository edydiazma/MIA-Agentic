"use client";

import { useEffect } from "react";
import { AVAILABILITY_LABEL, fmtNum, timeAgo } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useReport } from "@/components/reports/ReportPage";
import { BarList } from "@/components/reports/charts";
import type { RealtimeReport } from "@/components/reports/types";

const AVAIL_TONE = { available: "ok", away: "warn", busy: "bad" } as const;

export default function TiempoRealPage() {
  const { data, error, loading, reload } = useReport<RealtimeReport>("/api/reports/realtime");

  useEffect(() => {
    const t = setInterval(reload, 15000);
    return () => clearInterval(t);
  }, [reload]);
  useRealtime((event) => {
    if (event === "conversation.updated" || event === "agent.presence") reload();
  });

  const online = data?.agents.filter((a) => a.online).length ?? 0;
  return (
    <ReportPage
      title="En tiempo real"
      subtitle={data ? `Actualizado ${timeAgo(data.updated_at)} · se refresca cada 15 s` : undefined}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Con el bot" value={fmtNum(data.by_status.bot)} />
            <Stat label="Con asesor" value={fmtNum(data.by_status.human)} />
            <Stat
              label="Esperando asesor"
              value={fmtNum(data.waiting_unassigned)}
              tone={data.waiting_unassigned > 0 ? "warn" : "ok"}
              hint={data.waiting_unassigned ? `La más antigua: ${fmtNum(data.oldest_wait_minutes)} min` : "Nadie en cola"}
            />
            <Stat label="Asesores conectados" value={`${online} / ${data.agents.length}`} />
          </div>
          <div className="grid2">
            <Card title="Asesores">
              {data.agents.length === 0 ? (
                <Empty>No hay asesores activos</Empty>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Asesor</th>
                        <th>Estado</th>
                        <th>Grupos</th>
                        <th className="num">Abiertas</th>
                        <th className="num">Sin leer</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.agents.map((a) => (
                        <tr key={a.id}>
                          <td className="strong">{a.name}</td>
                          <td>
                            {a.online ? (
                              <Badge tone={AVAIL_TONE[a.availability]}>● {AVAILABILITY_LABEL[a.availability]}</Badge>
                            ) : (
                              <Badge>○ Desconectado</Badge>
                            )}
                          </td>
                          <td className="small muted">{a.groups.filter(Boolean).join(", ") || "—"}</td>
                          <td className="num">{fmtNum(a.open)}</td>
                          <td className="num">{a.unread ? <strong>{fmtNum(a.unread)}</strong> : "0"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Card title="Conversaciones con asesor por grupo">
              <BarList
                items={Object.entries(data.by_group).map(([label, value]) => ({ label, value }))}
                empty="No hay conversaciones con asesores ahora"
              />
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
