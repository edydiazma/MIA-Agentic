"use client";

import Link from "next/link";
import { fmtDateTime, fmtNum, timeAgo, type OutboundWebhook } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Stat, useApi } from "@/components/ui";

export default function WebhooksReportPage() {
  const { data, error, loading } = useApi<OutboundWebhook[]>("/api/outbound-webhooks");
  const forbidden = !!error && /administrador/i.test(error);
  const hooks = data ?? [];
  const failing = hooks.filter((w) => w.active && w.consecutive_failures > 0).length;

  return (
    <>
      <PageHeader
        title="Outbound · Webhooks"
        subtitle="Estado de entrega de los eventos que la plataforma envía a tus sistemas"
        actions={<Link href="/automatizaciones/webhooks">Configurar webhooks →</Link>}
      />
      {forbidden ? (
        <Card>
          <Empty>Este reporte solo está disponible para administradores.</Empty>
        </Card>
      ) : (
        <>
          <ErrorBox error={error} />
          {loading && !data ? (
            <Loading />
          ) : (
            <>
              <div className="stats">
                <Stat label="Webhooks" value={fmtNum(hooks.length)} />
                <Stat label="Activos" value={fmtNum(hooks.filter((w) => w.active).length)} />
                <Stat label="Con fallos recientes" value={fmtNum(failing)} tone={failing ? "warn" : undefined} />
                <Stat
                  label="Desactivados"
                  value={fmtNum(hooks.filter((w) => !w.active).length)}
                  tone={hooks.some((w) => !w.active) ? "bad" : undefined}
                />
              </div>
              <Card>
                {hooks.length === 0 ? (
                  <Empty>No hay webhooks configurados</Empty>
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Webhook</th>
                          <th>Estado</th>
                          <th className="num">Último código</th>
                          <th>Última entrega</th>
                          <th className="num">Fallos seguidos</th>
                          <th>Último error</th>
                        </tr>
                      </thead>
                      <tbody>
                        {hooks.map((w) => (
                          <tr key={w.id}>
                            <td>
                              <div className="strong">{w.name}</div>
                              <div className="small muted">{w.url}</div>
                            </td>
                            <td>
                              {!w.active ? (
                                <Badge tone="bad">✕ Desactivado</Badge>
                              ) : w.consecutive_failures ? (
                                <Badge tone="warn">⚠ Fallando</Badge>
                              ) : (
                                <Badge tone="ok">✓ Activo</Badge>
                              )}
                            </td>
                            <td className="num">{w.last_status ?? "—"}</td>
                            <td title={fmtDateTime(w.last_delivery_at)}>{timeAgo(w.last_delivery_at)}</td>
                            <td className="num">{fmtNum(w.consecutive_failures)}</td>
                            <td className="small muted" style={{ maxWidth: 280 }}>
                              {w.last_error ?? "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <p className="small muted" style={{ marginTop: 10 }}>
                  Tras 10 entregas fallidas seguidas el webhook se desactiva y se crea una alerta en Inicio.
                </p>
              </Card>
            </>
          )}
        </>
      )}
    </>
  );
}
