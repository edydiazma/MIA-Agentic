"use client";

import { useEffect, useState } from "react";
import { fmtDateTime, send, type Campaign } from "@/lib/api";
import { Card, Empty, ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import NewCampaignModal from "@/components/campaigns/NewCampaignModal";
import CampaignDetail from "@/components/campaigns/CampaignDetail";
import { CampaignStatusBadge, pct } from "@/components/campaigns/shared";

export default function CampanasPage() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const { data, error, loading, reload } = useApi<Campaign[]>("/api/campaigns");
  const [creating, setCreating] = useState(false);
  const [detail, setDetail] = useState<number | null>(null);
  const [run, busy, actionError] = useAction();

  const running = data?.some((c) => c.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setInterval(reload, 5000);
    return () => clearInterval(t);
  }, [running, reload]);

  async function start(c: Campaign) {
    if (!window.confirm(`¿Enviar «${c.name}» a ${c.total} destinatarios?`)) return;
    if (await run(() => send(`/api/campaigns/${c.id}/start`, "POST"))) reload();
  }

  async function remove(c: Campaign) {
    if (!window.confirm(`¿Eliminar el borrador «${c.name}»?`)) return;
    if (await run(() => send(`/api/campaigns/${c.id}`, "DELETE"))) reload();
  }

  return (
    <>
      <PageHeader
        title="Campañas"
        subtitle="Envíos masivos de plantillas aprobadas por Meta"
        actions={
          isAdmin && (
            <button className="primary" onClick={() => setCreating(true)}>
              Nueva campaña
            </button>
          )
        }
      />
      <Card>
        <ErrorBox error={error || actionError} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>Aún no hay campañas.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Campaña</th>
                  <th>Plantilla</th>
                  <th>Estado</th>
                  <th className="num">Enviados</th>
                  <th className="num">Entregados</th>
                  <th className="num">Leídos</th>
                  <th className="num">Fallidos</th>
                  <th className="num">No entregados</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((c) => (
                  <tr key={c.id} className="clickable" onClick={() => setDetail(c.id)}>
                    <td>
                      <div className="strong">{c.name}</div>
                      <div className="muted small">{fmtDateTime(c.created_at)}</div>
                    </td>
                    <td>{c.template_name}</td>
                    <td>
                      <CampaignStatusBadge status={c.status} />
                    </td>
                    <td className="num">{c.sent.toLocaleString("es")}</td>
                    <td className="num">
                      {c.delivered.toLocaleString("es")} <span className="muted small">{pct(c.delivered, c.sent)}</span>
                    </td>
                    <td className="num">{c.read.toLocaleString("es")}</td>
                    <td className="num">{c.failed.toLocaleString("es")}</td>
                    <td className="num">{c.not_delivered.toLocaleString("es")}</td>
                    <td className="right nowrap" onClick={(e) => e.stopPropagation()}>
                      {isAdmin && c.status === "draft" && (
                        <>
                          <button disabled={busy} onClick={() => start(c)}>
                            Enviar
                          </button>{" "}
                          <button className="danger" disabled={busy} onClick={() => remove(c)}>
                            Eliminar
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {creating && <NewCampaignModal onClose={() => setCreating(false)} onDone={reload} />}
      {detail !== null && <CampaignDetail id={detail} onClose={() => setDetail(null)} />}
    </>
  );
}
