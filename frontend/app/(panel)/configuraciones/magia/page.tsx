"use client";

import Link from "next/link";
import { send, type Bot, type ClassifierSettings } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

type CortexLite = { id: number; name: string; is_active: boolean };

/** Resumen de la IA: agentes (con su Cortex) y estado del clasificador. La edición vive en Cortex. */
export default function MagiaPage() {
  const isAdmin = useIsAdmin();
  const bots = useApi<Bot[]>("/api/bots");
  const cortexes = useApi<CortexLite[]>("/api/ai/cortexes");
  const classifier = useApi<ClassifierSettings>("/api/settings/classifier");
  const [run, busy, actionError] = useAction();
  const cortexName = (id: number | null) =>
    id == null ? "Cortex principal" : (cortexes.data ?? []).find((c) => c.id === id)?.name ?? `#${id}`;

  async function toggleBot(bot: Bot, enabled: boolean) {
    await run(() => send(`/api/bots/${bot.id}`, "PUT", { enabled }));
    bots.reload();
  }

  async function toggleClassifier(enabled: boolean) {
    await run(() => send("/api/settings/classifier", "PUT", { enabled }));
    classifier.reload();
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Magia de IA: agentes que atienden por WhatsApp y clasificación automática." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={bots.error || classifier.error || actionError} />

      <Card title="Agentes de IA" actions={<Link href="/automatizaciones/cortex">Configurar agentes →</Link>}>
        {bots.loading && !bots.data ? (
          <Loading />
        ) : !bots.data?.length ? (
          <Empty>No hay agentes de IA. Créalos en Automatizaciones → Cortex.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Agente</th><th>Cortex</th><th>Recursos</th><th>Números</th><th>Activo</th></tr>
              </thead>
              <tbody>
                {bots.data.map((b) => (
                  <tr key={b.id}>
                    <td><strong>{b.name}</strong>{b.description && <div className="small muted">{b.description}</div>}</td>
                    <td>{cortexName(b.cortex_id)}</td>
                    <td className="inline">
                      {b.use_knowledge && <Badge tone="info">Conocimiento</Badge>}
                      {b.use_memory && <Badge tone="info">Memoria</Badge>}
                      {b.use_customer_memory && <Badge tone="info">Memoria del cliente</Badge>}
                      {b.use_catalog && <Badge tone="info">Catálogo</Badge>}
                      {b.use_appointments && <Badge tone="info">Citas</Badge>}
                    </td>
                    <td>{b.channel_ids.length || "—"}</td>
                    <td>
                      <Toggle checked={b.enabled} onChange={(v) => isAdmin && !busy && toggleBot(b, v)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Clasificación con IA" actions={<Link href="/configuraciones/clasificacion">Configurar clasificación →</Link>}>
        {classifier.data ? (
          <div className="stack">
            <Toggle
              checked={classifier.data.enabled}
              onChange={(v) => isAdmin && !busy && toggleClassifier(v)}
              label={classifier.data.enabled ? "Activa: etiqueta, tipifica, enruta y llena la ficha" : "Desactivada"}
            />
            <p className="small muted" style={{ margin: 0 }}>Usa el {cortexName(classifier.data.cortex_id)}.</p>
          </div>
        ) : (
          <Loading />
        )}
      </Card>
    </>
  );
}
