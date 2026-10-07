"use client";

import Link from "next/link";
import { ORG_STATUS_LABEL, type OrgStatus, type PlatformMetrics } from "@/lib/saas-types";
import { Card, ErrorBox, Loading, PageHeader, Stat } from "@/components/ui";
import { usePlatform } from "@/components/saas/platform-api";

export default function PlatformDashboard() {
  const { data, error } = usePlatform<PlatformMetrics>("/metrics");
  return (
    <>
      <PageHeader title="Resumen" subtitle="Empresas, ingresos recurrentes y uso de este mes." />
      <ErrorBox error={error} />
      {!data ? (
        <Loading />
      ) : (
        <>
          <div className="stats">
            <Stat label="Empresas" value={data.organizations.toLocaleString("es")} />
            <Stat label="MRR" value={`US$ ${data.mrr_usd.toLocaleString("es")}`} hint="Suscripciones pagadas activas o en mora" />
            <Stat label="Conversaciones (mes)" value={data.conversations_month.toLocaleString("es")} />
            <Stat label="En prueba" value={(data.by_status.trial ?? 0).toLocaleString("es")} />
            <Stat
              label="Pago pendiente"
              value={(data.by_status.past_due ?? 0).toLocaleString("es")}
              tone={data.by_status.past_due ? "warn" : undefined}
            />
          </div>
          <div className="grid2">
            <Card title="Por estado" actions={<Link href="/plataforma/empresas">Ver empresas</Link>}>
              <table className="table">
                <tbody>
                  {Object.entries(data.by_status).map(([k, n]) => (
                    <tr key={k}>
                      <td>
                        <Link href={`/plataforma/empresas?status=${k}`}>{ORG_STATUS_LABEL[k as OrgStatus] ?? k}</Link>
                      </td>
                      <td className="num">{n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
            <Card title="Por plan">
              <table className="table">
                <tbody>
                  {Object.entries(data.by_plan).map(([k, n]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">{n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>
        </>
      )}
    </>
  );
}
