"use client";

import { useEffect } from "react";
import { AVAILABILITY_LABEL, fmtNum, timeAgo } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { Badge, Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useReport } from "@/components/reports/ReportPage";
import { BarList } from "@/components/reports/charts";
import type { RealtimeReport } from "@/components/reports/types";
import { fmtDuration } from "@/lib/supervision-types";

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
          {data.today && (
            <div className="stats">
              <Stat label="Entrantes hoy" value={fmtNum(data.today.incoming)} hint="Conversaciones nuevas desde las 00:00" />
              <Stat label="Transferidas hoy" value={fmtNum(data.today.handoffs)} />
              <Stat
                label="Atendidas hoy"
                value={fmtNum(data.today.attended)}
                hint={data.today.avg_first_response_s != null ? `Espera media ${fmtDuration(data.today.avg_first_response_s)}` : undefined}
              />
              <Stat label="Cerradas hoy" value={fmtNum(data.today.closed)} />
              <Stat label="Abandonadas hoy" value={fmtNum(data.today.abandoned)} tone={data.today.abandoned ? "bad" : "ok"} />
            </div>
          )}
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
                        <th>En el estado</th>
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
                            {!a.online ? (
                              <Badge>○ Desconectado</Badge>
                            ) : a.status ? (
                              <span className="inline small">
                                <span aria-hidden style={{ width: 9, height: 9, borderRadius: 9, background: a.status.color ?? "var(--muted)", display: "inline-block" }} />
                                {a.status.icon ? `${a.status.icon} ` : ""}{a.status.name}
                              </span>
                            ) : (
                              <Badge tone={AVAIL_TONE[a.availability]}>● {AVAILABILITY_LABEL[a.availability]}</Badge>
                            )}
                          </td>
                          <td className="small muted">{a.online && a.status ? fmtDuration(a.status.seconds) : "—"}</td>
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
            {data.groups && data.groups.length > 0 && (
              <Card title="Grupos">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Grupo</th>
                        <th className="num">Conectados</th>
                        <th className="num">Con asesor</th>
                        <th className="num">En cola</th>
                        <th className="num">Espera más larga</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.groups.map((g) => (
                        <tr key={g.group_id}>
                          <td className="strong">{g.name}</td>
                          <td className="num">{g.online} / {g.agents}</td>
                          <td className="num">{fmtNum(g.open)}</td>
                          <td className="num">{g.queue ? <Badge tone="warn">{fmtNum(g.queue)}</Badge> : "0"}</td>
                          <td className="num">{g.queue ? `${fmtNum(g.longest_wait_minutes)} min` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
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
