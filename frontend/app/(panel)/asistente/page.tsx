"use client";

import { useEffect, useRef, useState } from "react";
import { api, send } from "@/lib/api";
import type { AssistantMessage, AssistantThread } from "@/lib/copilot-types";
import { Card, ErrorBox, PageHeader, useApi } from "@/components/ui";
import { StackedDaily } from "@/components/reports/charts";

const TOOL_LABELS: Record<string, string> = {
  service_kpis: "Nivel de servicio",
  realtime: "Tiempo real",
  agents_performance: "Asesores",
  login_report: "Reporte Login",
  general_report: "Reporte general",
  quality: "Calidad",
  ads_performance: "Anuncios",
  channels: "Canales",
  customers: "Clientes",
  products: "Productos",
  copilot_adoption: "Copiloto",
};

const EXAMPLES = [
  "¿Cómo va el nivel de servicio esta semana?",
  "¿Qué asesor tiene más conversaciones abandonadas?",
  "¿Qué anuncio trae más clientes nuevos y a qué costo?",
  "¿Qué productos piden más este mes?",
];

export default function AssistantPage() {
  const threads = useApi<AssistantThread[]>("/api/assistant/threads");
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  async function open(id: number) {
    setThreadId(id);
    setError(null);
    try {
      const t = await api<AssistantThread & { messages: AssistantMessage[] }>(`/api/assistant/threads/${id}`);
      setMessages(t.messages);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function ask(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    setText("");
    const optimistic: AssistantMessage = {
      id: -Date.now(), role: "user", content: q, tool_calls: null, charts: null, created_at: new Date().toISOString(),
    };
    setMessages((m) => [...m, optimistic]);
    try {
      const r = await send<{ thread: AssistantThread; messages: AssistantMessage[] }>("/api/assistant/ask", "POST", {
        text: q, thread_id: threadId,
      });
      setThreadId(r.thread.id);
      setMessages((m) => [...m, ...r.messages]);
      threads.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setMessages((m) => m.filter((x) => x.id !== optimistic.id));
      setText(q);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: number) {
    await send(`/api/assistant/threads/${id}`, "DELETE").catch(() => undefined);
    if (id === threadId) {
      setThreadId(null);
      setMessages([]);
    }
    threads.reload();
  }

  return (
    <>
      <PageHeader
        title="Asistente"
        subtitle="Pregunta en lenguaje natural sobre la operación: responde con los mismos datos de los reportes y solo de tus grupos."
      />
      <ErrorBox error={threads.error} />
      <div className="assistant-layout">
        <Card title="Conversaciones">
          <div className="assistant-threads">
            <button type="button" className={threadId === null ? "active" : ""} onClick={() => { setThreadId(null); setMessages([]); }}>
              + Nueva pregunta
            </button>
            {(threads.data ?? []).map((t) => (
              <span key={t.id} className="inline" style={{ flexWrap: "nowrap" }}>
                <button type="button" className={t.id === threadId ? "active" : ""} style={{ flex: 1 }} onClick={() => open(t.id)}>
                  {t.title ?? "Sin título"}
                </button>
                <button type="button" className="icon" aria-label="Eliminar" title="Eliminar" onClick={() => remove(t.id)}>
                  ×
                </button>
              </span>
            ))}
          </div>
        </Card>
        <Card title={threadId ? "Conversación" : "Nueva pregunta"}>
          <div className="assistant-log" aria-live="polite">
            {messages.length === 0 && (
              <div className="inline">
                {EXAMPLES.map((e) => (
                  <button type="button" key={e} className="copilot-chip" onClick={() => ask(e)}>
                    {e}
                  </button>
                ))}
              </div>
            )}
            {messages.filter((m) => m.role !== "tool").map((m) => (
              <div key={m.id} className={`assistant-msg ${m.role}`}>
                {m.content}
                {m.role === "assistant" && m.tool_calls && m.tool_calls.length > 0 && (
                  <div className="small muted" style={{ marginTop: 6 }}>
                    Fuentes: {m.tool_calls.map((c) => `${TOOL_LABELS[c.name] ?? c.name}${c.ok ? "" : " (sin acceso)"}`).join(" · ")}
                  </div>
                )}
                {m.charts?.map((ch, i) => (
                  <div key={i} style={{ marginTop: 10 }}>
                    <StackedDaily title={TOOL_LABELS[ch.title] ?? ch.title} data={ch.data}
                      series={ch.keys.map((k) => ({ key: k, label: k }))} height={180} />
                  </div>
                ))}
              </div>
            ))}
            {busy && <div className="small muted">Consultando los reportes…</div>}
            <div ref={bottom} />
          </div>
          <form
            className="copilot-row"
            style={{ marginTop: 12 }}
            onSubmit={(e) => {
              e.preventDefault();
              ask(text);
            }}
          >
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Escribe tu pregunta…"
              aria-label="Pregunta para el asistente" />
            <button className="primary" disabled={busy || !text.trim()}>
              Preguntar
            </button>
          </form>
          <ErrorBox error={error} />
        </Card>
      </div>
    </>
  );
}
