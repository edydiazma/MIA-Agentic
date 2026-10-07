"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { fmtDate, qs } from "@/lib/api";
import { ORG_STATUS_LABEL, ORG_STATUS_TONE, type OrgStatus, type PlatformOrg } from "@/lib/saas-types";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader } from "@/components/ui";
import { usePlatform } from "@/components/saas/platform-api";

function OrgsTable() {
  const router = useRouter();
  const params = useSearchParams();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [search, setSearch] = useState("");
  const { data, error, loading } = usePlatform<PlatformOrg[]>(`/orgs${qs({ q: search, status })}`);

  return (
    <>
      <PageHeader title="Empresas" subtitle="Todas las empresas registradas, su plan y su consumo del mes." />
      <ErrorBox error={error} />
      <Card
        actions={
          <form
            className="inline"
            onSubmit={(e) => {
              e.preventDefault();
              setSearch(q);
            }}
          >
            <input placeholder="Buscar por nombre" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Todos los estados</option>
              {(Object.keys(ORG_STATUS_LABEL) as OrgStatus[]).map((s) => (
                <option key={s} value={s}>
                  {ORG_STATUS_LABEL[s]}
                </option>
              ))}
            </select>
            <button>Buscar</button>
          </form>
        }
      >
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>No hay empresas con ese filtro.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Empresa</th>
                  <th>Estado</th>
                  <th>Plan</th>
                  <th className="num">Usuarios</th>
                  <th className="num">Números</th>
                  <th className="num">Conversaciones (mes)</th>
                  <th>Alta</th>
                </tr>
              </thead>
              <tbody>
                {data.map((o) => (
                  <tr key={o.id} className="clickable" onClick={() => router.push(`/plataforma/empresas/${o.id}`)}>
                    <td>
                      <strong>{o.name}</strong>
                      <div className="small muted">{o.slug}</div>
                    </td>
                    <td>
                      <Badge tone={ORG_STATUS_TONE[o.status]}>{ORG_STATUS_LABEL[o.status]}</Badge>
                      {o.status === "trial" && o.trial_days_left != null && (
                        <div className="small muted">{Math.max(0, o.trial_days_left)} días</div>
                      )}
                    </td>
                    <td>{o.plan?.name ?? "—"}</td>
                    <td className="num">{o.users}</td>
                    <td className="num">{o.channels}</td>
                    <td className="num">{(o.usage.conversations ?? 0).toLocaleString("es")}</td>
                    <td>{fmtDate(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <OrgsTable />
    </Suspense>
  );
}
