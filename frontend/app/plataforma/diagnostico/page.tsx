"use client";

import { useEffect, useState } from "react";
import type { PreflightView } from "@/lib/preflight-types";
import { ErrorBox, Loading, PageHeader } from "@/components/ui";
import { papi, usePlatform } from "@/components/saas/platform-api";
import PreflightReport from "@/components/preflight/PreflightReport";

/** Back-office: verificaciones del servidor (infraestructura, Supabase, Meta, Stripe, SMTP, voz, IA). */
export default function PlatformDiagnosticoPage() {
  const { data, error, reload } = usePlatform<PreflightView>("/preflight");
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  useEffect(() => {
    if (!data?.run?.running) return;
    const t = setInterval(reload, 3000);
    return () => clearInterval(t);
  }, [data?.run?.running, reload]);

  async function verify(areas?: string[]) {
    setBusy(true);
    setRunError(null);
    try {
      await papi("/preflight/run", "POST", { areas });
      reload();
    } catch (e) {
      setRunError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Diagnóstico del servidor"
        subtitle="Variables del despliegue contra los servicios reales. También disponible por consola: python -m app.preflight (código de salida 0/1/2 para CI)."
      />
      <ErrorBox error={error || runError} />
      {!data && !error ? <Loading /> : <PreflightReport title="Plataforma" view={data} busy={busy} onRun={verify} />}
    </>
  );
}
