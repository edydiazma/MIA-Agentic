"use client";

import { useState } from "react";
import Link from "next/link";
import { send, type Group } from "@/lib/api";
import type { AIAgent } from "@/lib/ai-types";
import {
  DAY_LABEL,
  VOICES,
  type CallingChannel,
  type CallingHours,
  type VoiceAgent,
  type VoiceAgentInput,
} from "@/lib/voice-types";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import s from "@/components/voice/voice.module.css";

const BLANK: VoiceAgentInput = {
  name: "",
  enabled: true,
  ai_agent_id: null,
  provider: "openai_realtime",
  model: "gpt-realtime",
  voice: "alloy",
  language: "es",
  greeting: "Hola, gracias por llamar. ¿En qué te puedo ayudar?",
  max_duration_s: 600,
  transfer_group_id: null,
  record_calls: true,
  settings: {},
};
const DEFAULT_HOURS: CallingHours = { days: [0, 1, 2, 3, 4], start: "08:00", end: "18:00" };

function AgentForm({
  initial,
  aiAgents,
  groups,
  onClose,
  onSaved,
}: {
  initial: VoiceAgent | null;
  aiAgents: AIAgent[];
  groups: Group[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<VoiceAgentInput>(initial ? { ...initial } : BLANK);
  const [run, busy, error] = useAction();
  const set = <K extends keyof VoiceAgentInput>(k: K, v: VoiceAgentInput[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    const ok = await run(() =>
      initial ? send(`/api/voice/agents/${initial.id}`, "PUT", form) : send("/api/voice/agents", "POST", form),
    );
    if (ok) onSaved();
  }

  return (
    <Modal
      title={initial ? "Editar agente de voz" : "Nuevo agente de voz"}
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" onClick={save} disabled={busy || !form.name.trim()}>
            {busy ? "Guardando…" : "Guardar"}
          </button>
        </>
      }
    >
      <div className="form">
        <ErrorBox error={error} />
        <div className="grid2">
          <Field label="Nombre">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Recepción" />
          </Field>
          <Field label="Agente de IA" hint="Usa sus instrucciones, conocimiento y memoria para responder por voz.">
            <select value={form.ai_agent_id ?? ""} onChange={(e) => set("ai_agent_id", e.target.value ? Number(e.target.value) : null)}>
              <option value="">Sin agente (solo saludo y reglas de llamada)</option>
              {aiAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Saludo inicial" hint="Lo primero que dice al contestar.">
          <textarea rows={2} value={form.greeting} onChange={(e) => set("greeting", e.target.value)} />
        </Field>
        <div className="grid3">
          <Field label="Voz">
            <select value={form.voice} onChange={(e) => set("voice", e.target.value)}>
              {VOICES.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Idioma">
            <select value={form.language} onChange={(e) => set("language", e.target.value)}>
              <option value="es">Español</option>
              <option value="en">Inglés</option>
              <option value="pt">Portugués</option>
            </select>
          </Field>
          <Field label="Duración máxima (segundos)">
            <input
              type="number"
              min={30}
              max={3600}
              value={form.max_duration_s}
              onChange={(e) => set("max_duration_s", Number(e.target.value))}
            />
          </Field>
        </div>
        <div className="grid2">
          <Field label="Modelo" hint="Modelo de voz en tiempo real de OpenAI.">
            <input value={form.model} onChange={(e) => set("model", e.target.value)} />
          </Field>
          <Field label="Transferir a" hint="Grupo de asesores al que suena la llamada cuando el cliente pide un humano.">
            <select
              value={form.transfer_group_id ?? ""}
              onChange={(e) => set("transfer_group_id", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Cualquier asesor disponible</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="inline">
          <Toggle checked={form.enabled} onChange={(v) => set("enabled", v)} label="Activo" />
          <Toggle checked={form.record_calls} onChange={(v) => set("record_calls", v)} label="Grabar llamadas" />
        </div>
      </div>
    </Modal>
  );
}

function ChannelCalling({ channel, agents, isAdmin, onSaved }: { channel: CallingChannel; agents: VoiceAgent[]; isAdmin: boolean; onSaved: () => void }) {
  const [enabled, setEnabled] = useState(channel.calling_enabled);
  const [limitHours, setLimitHours] = useState(!!channel.calling_hours);
  const [hours, setHours] = useState<CallingHours>(channel.calling_hours ?? DEFAULT_HOURS);
  const [voiceAgent, setVoiceAgent] = useState<number | null>(channel.voice_agent_id);
  const [syncMeta, setSyncMeta] = useState(true);
  const [run, busy, error] = useAction();
  const [warn, setWarn] = useState<string | null>(null);

  const toggleDay = (d: number) =>
    setHours((h) => ({ ...h, days: h.days.includes(d) ? h.days.filter((x) => x !== d) : [...h.days, d].sort() }));

  async function save() {
    const r = await run(() =>
      send<CallingChannel & { meta_error: string | null }>(`/api/voice/channels/${channel.id}`, "PUT", {
        calling_enabled: enabled,
        calling_hours: limitHours ? hours : null,
        voice_agent_id: voiceAgent,
        sync_meta: syncMeta,
      }),
    );
    if (r) {
      setWarn(r.meta_error ? `Guardado aquí, pero Meta respondió: ${r.meta_error}` : null);
      onSaved();
    }
  }

  return (
    <Card
      title={
        <span className="inline">
          {channel.name || channel.display_phone || channel.phone_number_id}
          <Badge tone={channel.calling_enabled ? "ok" : "neutral"}>{channel.calling_enabled ? "Llamadas activas" : "Llamadas apagadas"}</Badge>
        </span>
      }
    >
      <div className="form">
        <ErrorBox error={error || warn} />
        <Toggle checked={enabled} onChange={setEnabled} label="Recibir llamadas de WhatsApp en este número" />
        <Field label="Quién contesta" hint="Sin agente de voz, la llamada suena en el navegador de los asesores disponibles.">
          <select value={voiceAgent ?? ""} onChange={(e) => setVoiceAgent(e.target.value ? Number(e.target.value) : null)} disabled={!isAdmin}>
            <option value="">Asesores (desde el navegador)</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id} disabled={!a.enabled}>
                🤖 {a.name}
                {a.enabled ? "" : " (inactivo)"}
              </option>
            ))}
          </select>
        </Field>
        <Toggle checked={limitHours} onChange={setLimitHours} label="Solo en horario de atención" />
        {limitHours && (
          <div className="stack">
            <div className={s.days} role="group" aria-label="Días">
              {DAY_LABEL.map((d, i) => (
                <button key={d} type="button" className={s.day} aria-pressed={hours.days.includes(i)} onClick={() => toggleDay(i)}>
                  {d}
                </button>
              ))}
            </div>
            <div className="inline">
              <input type="time" style={{ width: "auto" }} value={hours.start} onChange={(e) => setHours({ ...hours, start: e.target.value })} />
              <span className="muted">a</span>
              <input type="time" style={{ width: "auto" }} value={hours.end} onChange={(e) => setHours({ ...hours, end: e.target.value })} />
              <span className="small muted">(zona horaria de la empresa)</span>
            </div>
            <p className="small muted" style={{ margin: 0 }}>
              Fuera del horario la llamada se rechaza y el cliente recibe un mensaje con el horario.
            </p>
          </div>
        )}
        {isAdmin && (
          <div className="row">
            <Toggle checked={syncMeta} onChange={setSyncMeta} label="Aplicar también en Meta" />
            <button className="primary" onClick={save} disabled={busy}>
              {busy ? "Guardando…" : "Guardar"}
            </button>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function AgentesVozPage() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const agents = useApi<VoiceAgent[]>("/api/voice/agents");
  const channels = useApi<CallingChannel[]>("/api/voice/channels");
  const aiAgents = useApi<AIAgent[]>("/api/bots");
  const groups = useApi<Group[]>("/api/groups");
  const [editing, setEditing] = useState<VoiceAgent | "new" | null>(null);
  const [run, , error] = useAction();

  async function remove(a: VoiceAgent) {
    if (!confirm(`¿Eliminar el agente de voz "${a.name}"? Los números que lo usan pasarán a sonar en los asesores.`)) return;
    await run(() => send(`/api/voice/agents/${a.id}`, "DELETE"));
    agents.reload();
    channels.reload();
  }

  const aiName = (id: number | null) => aiAgents.data?.find((x) => x.id === id)?.name ?? "—";

  return (
    <>
      <PageHeader
        title="Agentes de voz"
        subtitle={
          <>
            Automatizaciones · Llamadas de WhatsApp atendidas por IA, con transferencia a un asesor ·{" "}
            <Link href="/automatizaciones/llamadas">Ver historial</Link>
          </>
        }
        actions={
          isAdmin && (
            <button className="primary" onClick={() => setEditing("new")}>
              + Nuevo agente de voz
            </button>
          )
        }
      />
      <ErrorBox error={error || agents.error || channels.error} />
      <Card title="Agentes de voz">
        {agents.loading && !agents.data ? (
          <Loading />
        ) : !agents.data?.length ? (
          <Empty>Aún no hay agentes de voz. Crea uno y asígnalo a un número para que conteste las llamadas.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Nombre</th>
                  <th>Agente de IA</th>
                  <th>Voz</th>
                  <th className="num">Máx.</th>
                  <th>Estado</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {agents.data.map((a) => (
                  <tr key={a.id}>
                    <td className="strong">{a.name}</td>
                    <td>{aiName(a.ai_agent_id)}</td>
                    <td>
                      {a.voice} · {a.language}
                    </td>
                    <td className="num">{Math.round(a.max_duration_s / 60)} min</td>
                    <td>
                      <Badge tone={a.enabled ? "ok" : "neutral"}>{a.enabled ? "Activo" : "Inactivo"}</Badge>{" "}
                      {a.record_calls && <Badge tone="info">Graba</Badge>}
                    </td>
                    {isAdmin && (
                      <td className="right nowrap">
                        <button className="link" onClick={() => setEditing(a)}>
                          Editar
                        </button>{" "}
                        · <button className="link danger" onClick={() => remove(a)}>
                          Eliminar
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 style={{ margin: "24px 0 12px" }}>Llamadas por número</h2>
      {channels.loading && !channels.data ? (
        <Loading />
      ) : (
        <div className="grid2">
          {(channels.data ?? []).map((c) => (
            <ChannelCalling
              key={`${c.id}-${c.calling_enabled}-${c.voice_agent_id}-${JSON.stringify(c.calling_hours)}`}
              channel={c}
              agents={agents.data ?? []}
              isAdmin={isAdmin}
              onSaved={channels.reload}
            />
          ))}
        </div>
      )}

      {editing && (
        <AgentForm
          initial={editing === "new" ? null : editing}
          aiAgents={aiAgents.data ?? []}
          groups={groups.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            agents.reload();
          }}
        />
      )}
    </>
  );
}
