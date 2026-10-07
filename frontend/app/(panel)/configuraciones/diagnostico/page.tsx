"use client";

import { useEffect } from "react";
import { send } from "@/lib/api";
import type { OrgPreflight } from "@/lib/preflight-types";
import { ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";
import PreflightReport from "@/components/preflight/PreflightReport";

export default function DiagnosticoPage() {
  const { data, error, reload } = useApi<OrgPreflight>("/api/preflight");
  const [run, busy, runError] = useAction();
  const running = !!data?.run?.running || !!data?.platform?.run?.running;

  // Una corrida larga sigue en el servidor: se consulta hasta que termine
  useEffect(() => {
    if (!running) return;
    const t = setInterval(reload, 3000);
    return () => clearInterval(t);
  }, [running, reload]);

  async function verify(areas?: string[]) {
    const r = await run(() => send<OrgPreflight>("/api/preflight/run", "POST", { areas }));
    if (r) reload();
  }

  return (
    <>
      <PageHeader
        title="Diagnóstico de integraciones"
        subtitle="Verifica con los servicios reales (Meta, Google Ads, CRM, correo, SSO, IA…) que todo esté listo para operar. Solo hace consultas de lectura: no envía mensajes ni cobra."
      />
      <ConfigTabs />
      <ErrorBox error={error || runError} />
      {!data && !error ? (
        <Loading />
      ) : (
        <>
          <PreflightReport title="Conexiones de tu empresa" view={data} busy={busy} onRun={verify} />
          {data?.platform && (
            <PreflightReport title="Servidor (variables del despliegue)" view={data.platform} busy={busy} onRun={verify} />
          )}
        </>
      )}
    </>
  );
}
