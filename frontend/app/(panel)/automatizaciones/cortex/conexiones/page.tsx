"use client";

import { useMemo } from "react";
import Link from "next/link";
import type { AIConnection, ConnectionHealth, Cortex } from "@/lib/ai-types";
import { ErrorBox, Loading, PageHeader, Stat, useApi } from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";
import ConnectionsPanel from "@/components/ai/ConnectionsPanel";
import CortexPanel from "@/components/ai/CortexPanel";
import CallsTable from "@/components/ai/CallsTable";

export default function ConnectionsPage() {
  const isAdmin = useIsAdmin();
  const connections = useApi<AIConnection[]>("/api/ai/connections");
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes");
  const conns = useMemo(() => connections.data ?? [], [connections.data]);
  // La salud (circuit breaker, p50/p95) viene dentro de cada conexión
  const health = useMemo(
    () => Object.fromEntries(conns.filter((c) => c.health).map((c) => [c.id, c.health as ConnectionHealth])),
    [conns],
  );
  const reload = () => {
    connections.reload();
    cortexes.reload();
  };
  const states = conns.map((c) => c.health?.state);

  return (
    <>
      <PageHeader
        title="Conexiones y failover"
        subtitle={
          <>
            Varias conexiones a LLMs por Cortex: si una falla, tarda más del presupuesto o responde fuera de rango, se usa la
            siguiente. Los agentes eligen su Cortex en <Link href="/automatizaciones/cortex">Agentes de IA</Link>.
          </>
        }
      />
      <AdminNotice />
      <ErrorBox error={connections.error || cortexes.error} />
      {connections.loading && !connections.data ? (
        <Loading />
      ) : (
        <>
          <div className="stats">
            <Stat label="Conexiones activas" value={conns.filter((c) => c.is_active).length} />
            <Stat label="Operativas" value={states.filter((s) => !s || s === "closed").length} tone="ok" />
            <Stat label="En pausa (circuit breaker)" value={states.filter((s) => s === "open").length}
              tone={states.some((s) => s === "open") ? "bad" : undefined} />
            <Stat label="Cortex" value={(cortexes.data ?? []).length} />
          </div>
          <ConnectionsPanel connections={conns} health={health} isAdmin={isAdmin} reload={reload} />
          <CortexPanel cortexes={cortexes.data ?? []} connections={conns} isAdmin={isAdmin} reload={reload} />
          <CallsTable connections={conns} cortexes={cortexes.data ?? []} />
        </>
      )}
    </>
  );
}
