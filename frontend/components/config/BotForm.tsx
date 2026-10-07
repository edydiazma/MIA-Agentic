"use client";

import { useEffect, useState } from "react";
import { send, type Bot } from "@/lib/api";
import { Card, ErrorBox, Field, Loading, Toggle, useAction, useApi } from "@/components/ui";
import { useIsAdmin } from "./common";

export const MODELS: Record<string, string[]> = {
  anthropic: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"],
  openai: [],
};
const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];

/** Primer bot de la cuenta (el MVP usa un bot por número). */
export function useBot() {
  const { data, error, loading, reload } = useApi<Bot[]>("/api/bots");
  return { bot: data?.[0] ?? null, error, loading, reload };
}

export default function BotForm({ onSaved }: { onSaved?: (b: Bot) => void }) {
  const isAdmin = useIsAdmin();
  const { bot, error, loading } = useBot();
  const [draft, setDraft] = useState<Bot | null>(null);
  const [run, busy, saveError] = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (bot) setDraft(bot);
  }, [bot]);

  if (loading && !draft) return <Loading />;
  if (!draft) return <ErrorBox error={error ?? "No hay bot configurado"} />;

  const set = <K extends keyof Bot>(k: K, v: Bot[K]) => setDraft({ ...draft, [k]: v });

  async function save() {
    const { id, ...body } = draft!;
    const r = await run(() => send<Bot>(`/api/bots/${id}`, "PUT", body));
    if (r) {
      setDraft(r);
      setSaved(new Date().toLocaleTimeString("es"));
      onSaved?.(r);
    }
  }

  return (
    <Card title="Configuración del agente">
      <div className="form">
        <Toggle
          checked={draft.enabled}
          onChange={(v) => set("enabled", v)}
          label={draft.enabled ? "Agente activo" : "Agente apagado: todas las conversaciones esperan a un asesor"}
        />
        <Field label="Nombre">
          <input value={draft.name} disabled={!isAdmin} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <div className="grid3">
          <Field label="Proveedor">
            <select value={draft.provider} disabled={!isAdmin} onChange={(e) => set("provider", e.target.value)}>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI</option>
            </select>
          </Field>
          <Field label="Modelo">
            <input list="bot-models" value={draft.model} disabled={!isAdmin} onChange={(e) => set("model", e.target.value)} />
            <datalist id="bot-models">
              {(MODELS[draft.provider] ?? []).map((m) => <option key={m} value={m} />)}
            </datalist>
          </Field>
          <Field label="Esfuerzo (solo Claude)" hint="Más esfuerzo = respuestas más pensadas, más lentas y costosas.">
            <select value={draft.effort ?? ""} disabled={!isAdmin} onChange={(e) => set("effort", e.target.value || null)}>
              {EFFORTS.map((x) => <option key={x} value={x}>{x || "por defecto"}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Instrucciones del agente (prompt de sistema)" hint="Quién es, qué vende, tono, qué debe pedir al cliente y cuándo transferir.">
          <textarea rows={16} value={draft.system_prompt} disabled={!isAdmin} onChange={(e) => set("system_prompt", e.target.value)} />
        </Field>
        <Field label="Mensaje al transferir a un asesor">
          <input value={draft.handoff_message} disabled={!isAdmin} onChange={(e) => set("handoff_message", e.target.value)} />
        </Field>
        <ErrorBox error={saveError} />
        {isAdmin && (
          <div className="inline">
            <button className="primary" onClick={save} disabled={busy}>{busy ? "Guardando…" : "Guardar"}</button>
            {saved && <span className="muted small">Guardado a las {saved}</span>}
          </div>
        )}
      </div>
    </Card>
  );
}
