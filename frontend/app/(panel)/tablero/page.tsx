"use client";

import { useEffect, useRef } from "react";
import { AVAILABILITY_LABEL, fmtNum, timeAgo, type Availability } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Stat, useApi } from "@/components/ui";

type Realtime = {
  updated_at: string;
  by_status: { bot: number; human: number };
  waiting_unassigned: number;
  oldest_wait_minutes: number;
  agents: {
    id: number;
    name: string;
    online: boolean;
    availability: Availability;
    groups: (string | null)[];
    open: number;
    unread: number;
  }[];
  by_group: Record<string, number>;
};

const AV_TONE: Record<Availability, "ok" | "warn" | "bad"> = { available: "ok", away: "warn", busy: "bad" };

export default function TableroPage() {
  const rt = useApi<Realtime>("/api/reports/realtime");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useRealtime((event) => {
    if (event !== "conversation.updated" && event !== "agent.presence") return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => rt.reload(), 1000);
  });
  useEffect(() => {
    const t = setInterval(() => rt.reload(), 20_000);
    return () => {
      clearInterval(t);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [rt.reload]);

  if (!rt.data) return rt.error ? <ErrorBox error={rt.error} /> : <Loading />;
  const d = rt.data;
  const agents = [...d.agents].sort((a, b) => Number(b.online) - Number(a.online) || b.open - a.open || a.name.localeCompare(b.name));
  const groups = Object.entries(d.by_group).sort((a, b) => b[1] - a[1]);
  const maxGroup = Math.max(1, ...groups.map(([, n]) => n));
  const online = d.agents.filter((a) => a.online).length;

  return (
    <>
      <PageHeader title="Tablero en tiempo real" subtitle={`Actualizado ${timeAgo(d.updated_at)}`} />
      <div className="stats">
        <Stat label="Atendidas por el bot" value={fmtNum(d.by_status.bot)} />
        <Stat label="Con asesor" value={fmtNum(d.by_status.human)} />
        <Stat
          label="En espera sin asignar"
          value={fmtNum(d.waiting_unassigned)}
          tone={d.waiting_unassigned ? "warn" : "ok"}
        />
        <Stat
          label="Espera más antigua"
          value={d.waiting_unassigned ? `${fmtNum(Math.round(d.oldest_wait_minutes))} min` : "—"}
          tone={d.oldest_wait_minutes > 10 ? "bad" : undefined}
        />
        <Stat label="Asesores conectados" value={`${online} / ${d.agents.length}`} />
      </div>

      <div className="grid2">
        <Card title="Asesores">
          {agents.length === 0 ? (
            <Empty>No hay asesores activos.</Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Asesor</th>
                    <th>Disponibilidad</th>
                    <th>Grupos</th>
                    <th className="num">Abiertas</th>
                    <th className="num">Sin leer</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.map((a) => (
                    <tr key={a.id}>
                      <td>
                        <span className="inline">
                          <span className={`dot ${a.online ? "on" : ""}`} title={a.online ? "Conectado" : "Desconectado"} />
                          {a.name}
                        </span>
                      </td>
                      <td>
                        {a.online ? (
                          <Badge tone={AV_TONE[a.availability]}>{AVAILABILITY_LABEL[a.availability]}</Badge>
                        ) : (
                          <Badge tone="neutral">Desconectado</Badge>
                        )}
                      </td>
                      <td className="small">{a.groups.filter(Boolean).join(", ") || <span className="muted">—</span>}</td>
                      <td className="num">{fmtNum(a.open)}</td>
                      <td className="num">{a.unread ? <span className="badge">{a.unread}</span> : "0"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Conversaciones con asesor por grupo">
          {groups.length === 0 ? (
            <Empty>No hay conversaciones con asesor en este momento.</Empty>
          ) : (
            <div className="bars">
              {groups.map(([name, n]) => (
                <div className="bar-row" key={name}>
                  <span className="preview">{name}</span>
                  <div className="bar-track">
                    <div className="bar-fill" style={{ width: `${(100 * n) / maxGroup}%` }} />
                  </div>
                  <span className="num right">{fmtNum(n)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
