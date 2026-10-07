"use client";

import { fmtDateTime, fmtNum } from "@/lib/api";
import { Card, Empty } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { type LoginReport, fmtDuration } from "@/lib/supervision-types";

const FALLBACK = ["#16a34a", "#dc2626", "#ca8a04", "#2563eb", "#9333ea", "#0891b2", "#9ca3af"];

export default function LoginPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<LoginReport>("/api/reports/login", range);
  const color = (key: string, i: number) => data?.statuses.find((s) => s.key === key)?.color || FALLBACK[i % FALLBACK.length];

  return (
    <ReportPage
      title="Reporte Login"
      subtitle="Tiempo de cada asesor por estado, tiempo laborado y primer ingreso / última salida por día."
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <Card title="Tiempo por estado">
            {data.agents.length === 0 ? (
              <Empty>No hay asesores</Empty>
            ) : (
              <>
                <div className="inline small" style={{ marginBottom: 10 }}>
                  {data.statuses.map((s, i) => (
                    <span key={s.key} className="inline">
                      <span aria-hidden style={{ width: 10, height: 10, borderRadius: 2, background: color(s.key, i), display: "inline-block" }} />
                      {s.name}
                      {s.counts_as_working ? "" : " (no laborado)"}
                    </span>
                  ))}
                </div>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Asesor</th>
                        <th className="num">Laborado</th>
                        <th className="num">Total registrado</th>
                        <th style={{ minWidth: 260 }}>Distribución</th>
                        {data.statuses.map((s) => (
                          <th key={s.key} className="num">{s.name}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {data.agents.map((a) => (
                        <tr key={a.agent_id}>
                          <td>
                            <span className="strong">{a.name}</span>
                            {a.employee_code && <div className="small muted">{a.employee_code}</div>}
                          </td>
                          <td className="num strong">{fmtDuration(a.worked_s)}</td>
                          <td className="num">{fmtDuration(a.total_s)}</td>
                          <td>
                            {a.total_s ? (
                              <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", background: "var(--panel-2)" }}>
                                {data.statuses.map((s, i) => {
                                  const sec = a.by_status[s.key] ?? 0;
                                  return sec ? (
                                    <div
                                      key={s.key}
                                      title={`${s.name}: ${fmtDuration(sec)}`}
                                      style={{ width: `${(100 * sec) / a.total_s}%`, background: color(s.key, i) }}
                                    />
                                  ) : null;
                                })}
                              </div>
                            ) : (
                              <span className="small muted">Sin registro</span>
                            )}
                          </td>
                          {data.statuses.map((s) => (
                            <td key={s.key} className="num small">{a.by_status[s.key] ? fmtDuration(a.by_status[s.key]) : "—"}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>
          <Card title="Ingresos por día">
            {data.agents.every((a) => a.sessions.length === 0) ? (
              <Empty>Sin sesiones registradas en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Asesor</th>
                      <th>Día</th>
                      <th>Primer ingreso</th>
                      <th>Última salida</th>
                      <th className="num">Sesiones</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.agents.flatMap((a) =>
                      a.sessions.map((s) => (
                        <tr key={`${a.agent_id}-${s.day}`}>
                          <td className="strong">{a.name}</td>
                          <td>{s.day}</td>
                          <td>{fmtDateTime(s.first_login)}</td>
                          <td>{s.last_logout ? fmtDateTime(s.last_logout) : "—"}</td>
                          <td className="num">{fmtNum(s.sessions)}</td>
                        </tr>
                      )),
                    )}
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
