"use client";

import Link from "next/link";
import type { Settings } from "@/lib/api";
import { Card, Field, Loading, PageHeader, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, useIsAdmin, useSetting } from "@/components/config/common";

export default function ReportesConfigPage() {
  const isAdmin = useIsAdmin();
  const { draft, set, save, busy, error, saved } = useSetting("reports");
  const conv = useApi<Settings["conversations"]>("/api/settings/conversations").data;

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Reportes: metas de nivel de servicio." />
      <ConfigTabs />
      <AdminNotice />
      {!draft ? (
        <Loading />
      ) : (
        <Card title="Nivel de servicio">
          <div className="form" style={{ maxWidth: 480 }}>
            <Field
              label="Meta de cumplimiento (%)"
              hint="Porcentaje de transferencias que deben recibir respuesta del asesor dentro del tiempo objetivo."
            >
              <input
                type="number"
                min={1}
                max={100}
                disabled={!isAdmin}
                value={draft.sla_target_pct}
                onChange={(e) => set("sla_target_pct", Number(e.target.value))}
              />
            </Field>
            <p className="small muted" style={{ margin: 0 }}>
              Tiempo objetivo de primera respuesta: <strong>{conv ? `${conv.sla_minutes} min` : "…"}</strong> (se cambia en{" "}
              <Link href="/configuraciones/conversaciones">Conversaciones</Link>).
            </p>
            {isAdmin && <SaveBar onSave={save} busy={busy} saved={saved} error={error} />}
          </div>
        </Card>
      )}
    </>
  );
}
