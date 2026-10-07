"use client";

import { useEffect } from "react";
import { fmtDateTime } from "@/lib/api";
import type { LearningRun } from "@/lib/ai-types";
import { Badge, Card, Empty } from "@/components/ui";

const STAT_LABEL: Record<string, string> = {
  conversations: "conversaciones",
  batches: "lotes",
  proposed: "propuestos",
  duplicates: "duplicados",
  failed_batches: "lotes fallidos",
  profile_id: "perfil",
};

/** Lista de ejecuciones de aprendizaje; refresca cada 4 s mientras alguna esté corriendo. */
export default function RunsList({
  runs,
  reload,
  title = "Ejecuciones",
}: {
  runs: LearningRun[];
  reload: () => void;
  title?: string;
}) {
  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setInterval(reload, 4000);
    return () => clearInterval(t);
  }, [running, reload]);

  return (
    <Card title={title}>
      {runs.length === 0 ? (
        <Empty>Aún no hay ejecuciones.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Inicio</th>
                <th>Estado</th>
                <th>Resultado</th>
                <th>Fin</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td className="nowrap small">{fmtDateTime(r.started_at)}</td>
                  <td>
                    <Badge tone={r.status === "done" ? "ok" : r.status === "failed" ? "bad" : "info"}>
                      {r.status === "done" ? "Terminada" : r.status === "failed" ? "Falló" : "En curso…"}
                    </Badge>
                  </td>
                  <td className="small">
                    {Object.entries(r.stats ?? {})
                      .filter(([, v]) => typeof v === "number" || typeof v === "string")
                      .map(([k, v]) => `${v} ${STAT_LABEL[k] ?? k}`)
                      .join(" · ") || "—"}
                    {r.error && <div className="error">{r.error}</div>}
                  </td>
                  <td className="nowrap small">{fmtDateTime(r.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
