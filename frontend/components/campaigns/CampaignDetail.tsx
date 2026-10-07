"use client";

import { useEffect, useState } from "react";
import { fmtDateTime, type CampaignDetail as Detail } from "@/lib/api";
import { ErrorBox, Loading, Modal, Stat, useApi } from "@/components/ui";
import { CampaignStatusBadge, RECIPIENT_LABEL, pct } from "./shared";

export default function CampaignDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, reload } = useApi<Detail>(`/api/campaigns/${id}`);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    if (data?.status !== "running") return;
    const t = setInterval(reload, 5000);
    return () => clearInterval(t);
  }, [data?.status, reload]);

  const rows = (data?.recipients ?? []).filter((r) => !filter || r.status === filter);

  return (
    <Modal wide title={data?.name ?? "Campaña"} onClose={onClose}>
      <ErrorBox error={error} />
      {!data ? (
        <Loading />
      ) : (
        <>
          <div className="inline">
            <CampaignStatusBadge status={data.status} />
            <span className="muted small">
              Plantilla <strong>{data.template_name}</strong> ({data.template_language}) · creada {fmtDateTime(data.created_at)}
              {data.finished_at && ` · terminó ${fmtDateTime(data.finished_at)}`}
            </span>
          </div>
          <div className="stats" style={{ marginBottom: 0 }}>
            <Stat label="Destinatarios" value={data.total.toLocaleString("es")} />
            <Stat label="Enviados" value={data.sent.toLocaleString("es")} />
            <Stat label="Entregados" value={data.delivered.toLocaleString("es")} hint={pct(data.delivered, data.sent)} tone="ok" />
            <Stat label="Leídos" value={data.read.toLocaleString("es")} hint={pct(data.read, data.delivered)} />
            <Stat label="Fallidos" value={data.failed.toLocaleString("es")} tone={data.failed ? "bad" : undefined} />
            <Stat label="No entregados" value={data.not_delivered.toLocaleString("es")} tone={data.not_delivered ? "warn" : undefined} />
          </div>
          <div className="row">
            <h2>Destinatarios</h2>
            <select style={{ maxWidth: 200 }} value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="">Todos los estados</option>
              {Object.entries(RECIPIENT_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Cliente</th>
                  <th>Teléfono</th>
                  <th>Estado</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{r.name ?? "—"}</td>
                    <td className="nowrap">+{r.wa_id}</td>
                    <td>{RECIPIENT_LABEL[r.status] ?? r.status}</td>
                    <td className="small error">{r.error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Modal>
  );
}
