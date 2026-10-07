"use client";

import Link from "next/link";
import { send } from "@/lib/api";
import { Badge, Card, ErrorBox, Loading, PageHeader, Toggle, useAction } from "@/components/ui";
import { useBot } from "@/components/config/BotForm";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";

export default function MagiaPage() {
  const isAdmin = useIsAdmin();
  const { bot, error, loading, reload } = useBot();
  const [run, busy, actionError] = useAction();

  async function toggle(enabled: boolean) {
    if (!bot) return;
    await run(() => send(`/api/bots/${bot.id}`, "PUT", { enabled }));
    reload();
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Magia de IA: el agente que atiende por WhatsApp." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={error || actionError} />
      {loading && !bot ? (
        <Loading />
      ) : bot ? (
        <Card title={bot.name} actions={<Link href="/automatizaciones/cortex">Configurar en Cortex →</Link>}>
          <div className="stack">
            <Toggle
              checked={bot.enabled}
              onChange={(v) => isAdmin && !busy && toggle(v)}
              label={bot.enabled ? "Agente activo" : "Agente apagado: las conversaciones esperan a un asesor"}
            />
            <dl className="kv">
              <dt>Proveedor</dt>
              <dd>{bot.provider === "anthropic" ? "Anthropic (Claude)" : "OpenAI"}</dd>
              <dt>Modelo</dt>
              <dd><code>{bot.model}</code></dd>
              <dt>Esfuerzo</dt>
              <dd>{bot.effort ?? "por defecto"}</dd>
              <dt>Capacidades</dt>
              <dd className="inline">
                <Badge tone="info">Texto</Badge>
                <Badge tone="info">Imágenes</Badge>
                <Badge tone="info">Notas de voz</Badge>
                <Badge tone="info">PDF</Badge>
                <Badge tone="info">Transferencia a asesor</Badge>
                <Badge tone="info">Citas</Badge>
              </dd>
            </dl>
            <p className="small muted" style={{ margin: 0 }}>
              Las instrucciones, el modelo y la base de conocimiento se editan en{" "}
              <Link href="/automatizaciones/cortex">Automatizaciones → Cortex</Link>. Las citas se activan en{" "}
              <Link href="/configuraciones/citas">Configuraciones → Citas</Link>.
            </p>
          </div>
        </Card>
      ) : null}
    </>
  );
}
