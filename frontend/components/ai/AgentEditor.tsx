"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, send, type Channel } from "@/lib/api";
import type { AIAgent, AIAgentIn, Cortex } from "@/lib/ai-types";
import { Card, ErrorBox, Field, Toggle, useAction, useApi } from "@/components/ui";
import JsonEditWithAI from "./JsonEditWithAI";
import AgentAdvancedConfig from "@/components/agent-config/AgentAdvancedConfig";
import { ADVANCED_DEFAULTS } from "@/lib/agent-config-types";

type Doc = { id: number; title: string; chars?: number; enabled: boolean; source_filename?: string | null };

const RESOURCES: [keyof AIAgentIn, string, string][] = [
  ["use_knowledge", "Base de conocimiento", "Documentos vinculados abajo"],
  ["use_memory", "Memoria del negocio", "Solo los elementos aprobados"],
  ["use_customer_memory", "Memoria del cliente", "Perfil vivo de cada cliente"],
  ["use_catalog", "Catálogo de productos", "Buscar y enviar productos"],
  ["use_appointments", "Citas", "Consultar disponibilidad y agendar (si las citas están activas)"],
];

export const EMPTY_AGENT: AIAgentIn = {
  name: "",
  description: "",
  enabled: false,
  cortex_id: null,
  system_prompt: "",
  handoff_message: "Te comunico con un asesor, en un momento te atiende.",
  use_knowledge: true,
  use_memory: true,
  use_customer_memory: true,
  use_catalog: true,
  use_appointments: true,
  ...ADVANCED_DEFAULTS,
};

/** Normaliza la respuesta de /api/bots/{id}/knowledge (lista de ids o de documentos). */
function docIds(data: unknown): number[] {
  const list = Array.isArray(data) ? data : ((data as { doc_ids?: unknown[] })?.doc_ids ?? []);
  return list.map((d) => (typeof d === "number" ? d : (d as { id: number }).id));
}

export default function AgentEditor({
  agent,
  template,
  isAdmin,
  onSaved,
}: {
  agent: AIAgent | null; // null = nuevo
  template?: AIAgentIn | null; // datos iniciales de un agente nuevo (duplicar)
  isAdmin: boolean;
  onSaved: (a: AIAgent) => void;
}) {
  const blank = () => ({ ...EMPTY_AGENT, ...(template ?? {}) });
  const [form, setForm] = useState<AIAgentIn>(agent ? { ...ADVANCED_DEFAULTS, ...agent } : blank());
  const [links, setLinks] = useState<number[]>([]);
  const [channels, setChannels] = useState<number[]>(agent?.channel_ids ?? []);
  const [aiEdit, setAiEdit] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [run, busy, error] = useAction();
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes");
  const docs = useApi<Doc[]>("/api/knowledge");
  const chans = useApi<{ channels: Channel[] }>("/api/channels");
  const upload = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setForm(agent ? { ...ADVANCED_DEFAULTS, ...agent } : blank());
    setChannels(agent?.channel_ids ?? []);
    setSaved(null);
    if (agent) api<unknown>(`/api/bots/${agent.id}/knowledge`).then((d) => setLinks(docIds(d))).catch(() => setLinks([]));
    else setLinks([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent]);

  const set = <K extends keyof AIAgentIn>(k: K, v: AIAgentIn[K]) => setForm((f) => ({ ...f, [k]: v }));
  const toggleLink = (id: number) => setLinks((l) => (l.includes(id) ? l.filter((x) => x !== id) : [...l, id]));
  const toggleChannel = (id: number) => setChannels((l) => (l.includes(id) ? l.filter((x) => x !== id) : [...l, id]));

  async function save() {
    const r = await run(async () => {
      const body = { ...form, cortex_id: form.cortex_id || null };
      const a = agent
        ? await send<AIAgent>(`/api/bots/${agent.id}`, "PUT", body)
        : await send<AIAgent>("/api/bots", "POST", body);
      await send(`/api/bots/${a.id}/knowledge`, "PUT", { ids: links });
      return send<AIAgent>(`/api/bots/${a.id}/channels`, "PUT", { ids: channels });
    });
    if (r) {
      setSaved(new Date().toLocaleTimeString("es"));
      onSaved(r);
    }
  }

  async function uploadDoc(f: File) {
    const fd = new FormData();
    fd.append("file", f);
    if (agent) fd.append("bot_id", String(agent.id));
    const d = await run(() => api<Doc>("/api/knowledge/upload", { method: "POST", body: fd }));
    if (d) {
      await docs.reload();
      setLinks((l) => [...l, d.id]);
    }
  }

  const disabled = !isAdmin || busy;
  const totalChars = (docs.data ?? []).filter((d) => links.includes(d.id)).reduce((a, d) => a + (d.chars ?? 0), 0);

  return (
    <>
      <Card
        title={agent ? `Agente: ${agent.name}` : "Nuevo agente de IA"}
        actions={
          agent && isAdmin ? <button onClick={() => setAiEdit(true)}>✨ Editar con IA</button> : undefined
        }
      >
        <div className="form">
          <div className="grid2">
            <Field label="Nombre">
              <input value={form.name} disabled={disabled} onChange={(e) => set("name", e.target.value)} />
            </Field>
            <Field label="Cortex (conexiones de IA con failover)" hint="Vacío = el Cortex de chat principal.">
              <select
                value={form.cortex_id ?? ""}
                disabled={disabled}
                onChange={(e) => set("cortex_id", e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">Automático</option>
                {(cortexes.data ?? []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.strategy}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Field label="Descripción">
            <input value={form.description ?? ""} disabled={disabled} onChange={(e) => set("description", e.target.value)} />
          </Field>
          <Toggle checked={form.enabled} onChange={(v) => isAdmin && set("enabled", v)} label="Agente activo (responde en los canales asignados)" />
          <Field label="Instrucciones (prompt de sistema)">
            <textarea rows={14} value={form.system_prompt} disabled={disabled} onChange={(e) => set("system_prompt", e.target.value)} />
          </Field>
          <Field label="Mensaje al transferir a un asesor">
            <input value={form.handoff_message} disabled={disabled} onChange={(e) => set("handoff_message", e.target.value)} />
          </Field>
        </div>
      </Card>

      <AgentAdvancedConfig form={form} set={set} disabled={disabled} isAdmin={isAdmin} />

      <Card title="Recursos conectados">
        <div className="grid2">
          {RESOURCES.map(([k, label, hint]) => (
            <div key={k} className="stack" style={{ gap: 2 }}>
              <Toggle checked={Boolean(form[k])} onChange={(v) => isAdmin && set(k, v as never)} label={label} />
              <small className="muted">{hint}</small>
            </div>
          ))}
        </div>
      </Card>

      <Card
        title="Documentos anteriores vinculados"
        actions={
          isAdmin ? (
            <>
              <Link href="/automatizaciones/cortex/conocimiento" className="button">
                Base de conocimiento (RAG) →
              </Link>
              <button onClick={() => upload.current?.click()} disabled={busy}>⬆ Subir PDF / txt / md</button>
              <input
                ref={upload}
                type="file"
                hidden
                accept=".pdf,.txt,.md,.csv,.json"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (f) uploadDoc(f);
                }}
              />
            </>
          ) : undefined
        }
      >
        {!form.use_knowledge && <p className="muted small">La base de conocimiento está desactivada para este agente.</p>}
        <p className="muted small">
          Las fuentes nuevas (archivos, sitio web, catálogo, conversaciones, preguntas frecuentes) se administran en{" "}
          <Link href="/automatizaciones/cortex/conocimiento">Base de conocimiento</Link> y se consultan con búsqueda semántica.
        </p>
        {(docs.data ?? []).length === 0 ? (
          <p className="muted">No hay documentos. Sube uno para compartirlo entre agentes.</p>
        ) : (
          <div className="stack" style={{ gap: 6 }}>
            {(docs.data ?? []).map((d) => (
              <label key={d.id} className="inline">
                <input type="checkbox" checked={links.includes(d.id)} disabled={disabled} onChange={() => toggleLink(d.id)} />
                <span>{d.title}</span>
                {d.chars != null && <span className="muted small">{d.chars.toLocaleString("es")} caracteres</span>}
                {!d.enabled && <span className="muted small">(desactivado)</span>}
              </label>
            ))}
            <small className={totalChars > 600_000 ? "error" : "muted"}>
              Total vinculado: {totalChars.toLocaleString("es")} caracteres
              {totalChars > 600_000 && " — es mucho contexto; considera dividir entre agentes"}
            </small>
          </div>
        )}
      </Card>

      <Card title="Canales de WhatsApp que atiende">
        {(chans.data?.channels ?? []).length === 0 ? (
          <p className="muted">No hay números configurados (Configuraciones → Plataforma).</p>
        ) : (
          <div className="stack" style={{ gap: 6 }}>
            {(chans.data?.channels ?? []).map((c) => (
              <label key={c.id} className="inline">
                <input type="checkbox" checked={channels.includes(c.id)} disabled={disabled} onChange={() => toggleChannel(c.id)} />
                <span>{c.name}</span>
                <span className="muted small">{c.display_phone || c.phone_number_id}</span>
              </label>
            ))}
            <small className="muted">Cada número tiene un solo agente por defecto: asignarlo aquí lo quita del agente anterior.</small>
          </div>
        )}
      </Card>

      <ErrorBox error={error} />
      {isAdmin && (
        <div className="row">
          <button className="primary" disabled={busy || !form.name.trim() || !form.system_prompt.trim()} onClick={save}>
            {busy ? "Guardando…" : agent ? "Guardar cambios" : "Crear agente"}
          </button>
          {saved && <span className="muted">Guardado a las {saved}</span>}
        </div>
      )}

      {aiEdit && agent && (
        <JsonEditWithAI
          entityType="ai_agent"
          entityId={agent.id}
          title={`Editar «${agent.name}» con IA`}
          onClose={() => setAiEdit(false)}
          onApplied={async () => onSaved(await api<AIAgent>(`/api/bots/${agent.id}`).catch(() => agent))}
        />
      )}
    </>
  );
}
