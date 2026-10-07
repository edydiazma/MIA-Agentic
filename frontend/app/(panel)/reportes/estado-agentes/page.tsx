"use client";

import { useEffect } from "react";
import { AVAILABILITY_LABEL, fmtNum } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import type { AgentsReport, RealtimeReport } from "@/components/reports/types";

const AVAIL_TONE = { available: "ok", away: "warn", busy: "bad" } as const;

export default function EstadoAgentesPage() {
  const [range, setRange] = useDateRange();
  const report = useReport<AgentsReport>("/api/reports/agents", range);
  const live = useReport<RealtimeReport>("/api/reports/realtime");
  const { reload: reloadLive } = live;
  const { reload: reloadReport } = report;

  useEffect(() => {
    const t = setInterval(reloadLive, 15000);
    return () => clearInterval(t);
  }, [reloadLive]);
  useRealtime((event) => {
    if (event === "agent.presence") {
      reloadLive();
      reloadReport();
    } else if (event === "conversation.updated") reloadLive();
  });

  const loadById = new Map((live.data?.agents ?? []).map((a) => [a.id, a]));
  const agents = report.data?.agents ?? [];
  const active = agents.filter((a) => a.is_active);
  const online = active.filter((a) => a.online);
  const count = (v: string) => online.filter((a) => a.availability === v).length;

  return (
    <ReportPage
      title="Estado de agentes"
      subtitle="Conexión y disponibilidad actual de cada asesor, su carga y su actividad en el periodo"
      range={range}
      onRange={setRange}
      loading={report.loading}
      error={report.error ?? live.error}
      ready={!!report.data}
    >
      {report.data && (
        <>
          <div className="stats">
            <Stat label="Conectados" value={`${online.length} / ${active.length}`} />
            <Stat label="Disponibles" value={fmtNum(count("available"))} tone="ok" />
            <Stat label="Ausentes" value={fmtNum(count("away"))} />
            <Stat label="Ocupados" value={fmtNum(count("busy"))} />
          </div>
          <Card title="Asesores">
            {agents.length === 0 ? (
              <Empty>No hay asesores</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Asesor</th>
                      <th>Conexión</th>
                      <th>Disponibilidad</th>
                      <th>Cuenta</th>
                      <th className="num">Abiertas ahora</th>
                      <th className="num">Sin leer</th>
                      <th className="num">Mensajes en el periodo</th>
                      <th className="num">Cerradas en el periodo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agents.map((a) => {
                      const l = loadById.get(a.id);
                      return (
                        <tr key={a.id}>
                          <td className="strong">{a.name}</td>
                          <td>{a.online ? <Badge tone="ok">● Conectado</Badge> : <Badge>○ Desconectado</Badge>}</td>
                          <td>
                            <Badge tone={a.online ? AVAIL_TONE[a.availability] : "neutral"}>
                              {AVAILABILITY_LABEL[a.availability]}
                            </Badge>
                          </td>
                          <td>{a.is_active ? "Activa" : <span className="muted">Inactiva</span>}</td>
                          <td className="num">{fmtNum(l?.open ?? 0)}</td>
                          <td className="num">{fmtNum(l?.unread ?? 0)}</td>
                          <td className="num">{fmtNum(a.messages_sent)}</td>
                          <td className="num">{fmtNum(a.conversations_closed)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <p className="small muted" style={{ marginTop: 10 }}>
              La asignación automática solo considera a los asesores conectados y «Disponibles».
            </p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
