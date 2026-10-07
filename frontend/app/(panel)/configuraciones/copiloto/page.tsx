"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { send } from "@/lib/api";
import type { Cortex } from "@/lib/ai-types";
import type { CopilotSettings } from "@/lib/copilot-types";
import { Card, ErrorBox, Field, Loading, PageHeader, Toggle, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, useIsAdmin } from "@/components/config/common";

export default function CopilotSettingsPage() {
  const isAdmin = useIsAdmin();
  const { data, error } = useApi<CopilotSettings>("/api/settings/copilot");
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes").data ?? [];
  const [draft, setDraft] = useState<CopilotSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  const set = <K extends keyof CopilotSettings>(k: K, v: CopilotSettings[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d));

  async function save() {
    if (!draft) return;
    setBusy(true);
    setSaveError(null);
    try {
      setDraft(await send<CopilotSettings>("/api/settings/copilot", "PUT", draft));
      setSaved(new Date().toLocaleTimeString("es"));
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const cortexSelect = (key: "reply_cortex_id" | "summary_cortex_id" | "assistant_cortex_id", hint: string) => (
    <select disabled={!isAdmin} value={draft?.[key] ?? ""} onChange={(e) => set(key, e.target.value ? Number(e.target.value) : null)}
      aria-label={hint}>
      <option value="">Automático ({hint})</option>
      {cortexes.filter((c) => c.is_active).map((c) => (
        <option key={c.id} value={c.id}>{c.name}</option>
      ))}
    </select>
  );

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Copiloto de IA: sugerencias para asesores, resúmenes y asistente del supervisor." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={error} />
      {!draft ? (
        <Loading />
      ) : (
        <>
          <Card title="Copiloto del asesor">
            <div className="form" style={{ maxWidth: 560 }}>
              <Toggle checked={draft.enabled} onChange={(v) => isAdmin && set("enabled", v)} label="Copiloto activo" />
              <Field label="Sugerencias de respuesta"
                hint="Automáticas: al llegar un mensaje del cliente a una conversación con asesor. A pedido: solo con «↻ Sugerir».">
                <select disabled={!isAdmin} value={draft.suggestions}
                  onChange={(e) => set("suggestions", e.target.value as CopilotSettings["suggestions"])}>
                  <option value="auto">Automáticas</option>
                  <option value="on_demand">A pedido del asesor</option>
                </select>
              </Field>
              <div className="grid2">
                <Field label="Opciones por mensaje">
                  <input type="number" min={1} max={5} disabled={!isAdmin} value={draft.max_suggestions}
                    onChange={(e) => set("max_suggestions", Number(e.target.value))} />
                </Field>
                <Field label="Máximo por conversación y hora" hint="Controla el costo en conversaciones muy activas.">
                  <input type="number" min={1} max={100} disabled={!isAdmin} value={draft.max_per_conversation_per_hour}
                    onChange={(e) => set("max_per_conversation_per_hour", Number(e.target.value))} />
                </Field>
                <Field label="Esperar ráfagas (segundos)" hint="Si el cliente envía varios mensajes seguidos, sugiere una sola vez.">
                  <input type="number" min={0} max={60} disabled={!isAdmin} value={draft.debounce_seconds}
                    onChange={(e) => set("debounce_seconds", Number(e.target.value))} />
                </Field>
                <Field label="Mensajes de contexto">
                  <input type="number" min={5} max={80} disabled={!isAdmin} value={draft.history_messages}
                    onChange={(e) => set("history_messages", Number(e.target.value))} />
                </Field>
              </div>
              <Toggle checked={draft.next_action} onChange={(v) => isAdmin && set("next_action", v)}
                label="Siguiente mejor acción (enviar producto, ofrecer cita, pedir un dato, mover etapa…)" />
              <Toggle checked={draft.handoff_summary} onChange={(v) => isAdmin && set("handoff_summary", v)}
                label="Resumen para el asesor al transferir" />
              <Toggle checked={draft.close_summary} onChange={(v) => isAdmin && set("close_summary", v)}
                label="Resumen de la conversación al cerrar" />
              <Toggle checked={draft.assistant} onChange={(v) => isAdmin && set("assistant", v)}
                label="Asistente para supervisores" />
            </div>
          </Card>
          <Card title="Modelos">
            <div className="form" style={{ maxWidth: 560 }}>
              <p className="small muted" style={{ margin: 0 }}>
                Elige un Cortex rápido para las sugerencias (el asesor las espera) y uno más potente para resúmenes y el
                asistente. Se configuran en <Link href="/automatizaciones/cortex/conexiones">Conexiones y failover</Link>.
              </p>
              <Field label="Sugerencias, borradores y reescritura">{cortexSelect("reply_cortex_id", "propósito Copiloto")}</Field>
              <Field label="Resúmenes">{cortexSelect("summary_cortex_id", "propósito Copiloto")}</Field>
              <Field label="Asistente del supervisor">{cortexSelect("assistant_cortex_id", "propósito Asistente")}</Field>
              <p className="small muted" style={{ margin: 0 }}>
                Las sugerencias nunca inventan precios ni disponibilidad: las cifras que no estén en el catálogo, el
                conocimiento o la conversación se descartan. El uso se mide en{" "}
                <Link href="/reportes/copiloto">Reportes → Copiloto</Link>.
              </p>
              {isAdmin && <SaveBar onSave={save} busy={busy} saved={saved} error={saveError} />}
            </div>
          </Card>
        </>
      )}
    </>
  );
}
