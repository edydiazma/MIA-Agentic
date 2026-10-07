"use client";

import { useEffect, useRef, useState } from "react";
import { api, send } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import {
  REWRITE_LABELS,
  type CopilotOverview,
  type CopilotSuggestion,
  type ExecuteResult,
  type RewriteMode,
} from "@/lib/copilot-types";

/** Sugerencia elegida por el asesor: al enviar, el chat reporta si la mandó igual o editada. */
export type CopilotPick = { id: number; text: string };

type Props = {
  conversationId: number;
  /** Solo se piden sugerencias cuando un asesor atiende la conversación. */
  active: boolean;
  text: string;
  setText: (t: string) => void;
  onPick: (p: CopilotPick | null) => void;
  onOpenTemplate: () => void;
  onOpenTransfer: () => void;
};

/**
 * Barra del copiloto sobre el compositor: resumen de traspaso, respuestas sugeridas (clic = insertar y editar),
 * siguiente mejor acción, «Redactar» con instrucción y «Reescribir». Atajos: Alt+1/2/3 inserta una sugerencia,
 * Alt+R redacta, Esc descarta.
 */
export default function CopilotBar({ conversationId, active, text, setText, onPick, onOpenTemplate, onOpenTransfer }: Props) {
  const [data, setData] = useState<CopilotOverview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(true);
  const [hidden, setHidden] = useState(false);
  const lastTyping = useRef(0);

  useEffect(() => {
    let alive = true;
    setData(null);
    setHidden(false);
    setError(null);
    api<CopilotOverview>(`/api/conversations/${conversationId}/copilot`)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null));
    return () => {
      alive = false;
    };
  }, [conversationId]);

  // Sugerencias, siguiente acción y resumen llegan en vivo (solo al asesor asignado)
  useRealtime((event, payload) => {
    const d = payload as CopilotSuggestion & { conversation_id?: number };
    if (!d || d.conversation_id !== conversationId) return;
    if (event === "copilot.suggestion") {
      setData((prev) => (prev ? { ...prev, reply: d, fresh: true, pending: false } : prev));
      setHidden(false);
    } else if (event === "copilot.next_action") {
      setData((prev) => (prev ? { ...prev, next_action: d } : prev));
    } else if (event === "copilot.summary") {
      const t = (d.content as { text?: string }).text ?? null;
      setData((prev) => (prev ? { ...prev, handoff_summary: t } : prev));
      setSummaryOpen(true);
    }
  });

  // «El asesor está escribiendo»: el copiloto no interrumpe con sugerencias nuevas (máx. 1 aviso cada 4 s)
  useEffect(() => {
    if (!active || !text) return;
    const now = Date.now();
    if (now - lastTyping.current < 4000) return;
    lastTyping.current = now;
    send(`/api/conversations/${conversationId}/copilot/typing`, "POST", { typing: true }).catch(() => undefined);
  }, [text, active, conversationId]);

  const options = !hidden ? data?.reply?.content.options ?? [] : [];

  // Atajos de teclado
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!e.altKey) return;
      const n = Number(e.key);
      if (n >= 1 && n <= options.length) {
        e.preventDefault();
        insert(options[n - 1]);
      } else if (e.key.toLowerCase() === "r") {
        e.preventDefault();
        setDrafting(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!data || !data.enabled) return null;

  function insert(option: string) {
    setText(option);
    if (data?.reply) onPick({ id: data.reply.id, text: option });
  }

  async function run<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(key);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(null);
    }
  }

  async function refresh() {
    const r = await run("refresh", () =>
      send<{ reply?: CopilotSuggestion; next_action?: CopilotSuggestion | null; skipped?: string }>(
        `/api/conversations/${conversationId}/copilot/refresh`, "POST"),
    );
    if (!r) return;
    if (r.skipped === "rate_limited") setError("Alcanzaste el límite de sugerencias por hora en esta conversación.");
    if (r.reply) setData((prev) => (prev ? { ...prev, reply: r.reply ?? null, next_action: r.next_action ?? prev.next_action, fresh: true } : prev));
    setHidden(false);
  }

  async function dismiss() {
    if (data?.reply) await send(`/api/copilot/suggestions/${data.reply.id}/outcome`, "POST", { status: "dismissed" }).catch(() => undefined);
    setHidden(true);
    onPick(null);
  }

  async function draft() {
    const r = await run("draft", () =>
      send<CopilotSuggestion>(`/api/conversations/${conversationId}/copilot/draft`, "POST", { instruction: instruction || null }),
    );
    if (r?.content.text) {
      setText(r.content.text);
      onPick({ id: r.id, text: r.content.text });
      setDrafting(false);
      setInstruction("");
      if (r.content.flags?.length) setError(`Revisa antes de enviar: ${r.content.flags.join("; ")}`);
    }
  }

  async function rewrite(mode: RewriteMode) {
    setRewriteOpen(false);
    if (!text.trim()) return;
    const r = await run("rewrite", () =>
      send<{ text: string; flags: string[] }>("/api/copilot/rewrite", "POST", {
        text, mode, target_lang: mode === "translate" ? "inglés" : null, conversation_id: conversationId,
      }),
    );
    if (r) {
      setText(r.text);
      if (r.flags.length) setError(`No se aplicó: ${r.flags.join("; ")}`);
    }
  }

  async function executeAction() {
    const na = data?.next_action;
    if (!na) return;
    const r = await run("action", () => send<ExecuteResult>(`/api/copilot/suggestions/${na.id}/execute`, "POST"));
    if (!r) return;
    if (r.client_action === "open_template") onOpenTemplate();
    else if (r.client_action === "open_transfer") onOpenTransfer();
    if (r.insert_text) setText(r.insert_text);
    setData((prev) => (prev ? { ...prev, next_action: null } : prev));
  }

  const na = data.next_action && data.next_action.status === "shown" ? data.next_action : null;

  return (
    <div className="copilot" aria-label="Copiloto">
      {data.handoff_summary && (
        <div className="copilot-summary">
          <button type="button" className="link small" onClick={() => setSummaryOpen((v) => !v)} aria-expanded={summaryOpen}>
            ✨ Resumen del traspaso {summaryOpen ? "▾" : "▸"}
          </button>
          {summaryOpen && <pre className="copilot-summary-text">{data.handoff_summary}</pre>}
        </div>
      )}
      {active && (
        <div className="copilot-row">
          {options.map((o, i) => (
            <button
              type="button"
              key={i}
              className="copilot-chip"
              title={`Alt+${i + 1} · clic para insertar y editar`}
              onClick={() => insert(o)}
            >
              <span className="copilot-n">{i + 1}</span> {o}
            </button>
          ))}
          {data.pending && !options.length && <span className="small muted">✨ Preparando sugerencias…</span>}
          {na && (
            <button
              type="button"
              className="copilot-chip action"
              title={na.content.reason ?? ""}
              disabled={busy === "action"}
              onClick={executeAction}
            >
              ⚡ {na.content.label}
            </button>
          )}
          <span className="copilot-tools">
            {options.length > 0 && (
              <button type="button" className="link small" onClick={dismiss} title="Descartar sugerencias">
                Descartar
              </button>
            )}
            <button type="button" className="link small" disabled={busy === "refresh"} onClick={refresh}>
              {busy === "refresh" ? "…" : "↻ Sugerir"}
            </button>
            <button type="button" className="link small" onClick={() => setDrafting((v) => !v)} title="Alt+R">
              ✨ Redactar
            </button>
            <span className="copilot-menu">
              <button type="button" className="link small" disabled={!text.trim() || busy === "rewrite"}
                onClick={() => setRewriteOpen((v) => !v)} aria-expanded={rewriteOpen}>
                Reescribir ▾
              </button>
              {rewriteOpen && (
                <span className="copilot-menu-list" role="menu">
                  {(Object.keys(REWRITE_LABELS) as RewriteMode[]).map((m) => (
                    <button type="button" role="menuitem" key={m} onClick={() => rewrite(m)}>
                      {REWRITE_LABELS[m]}
                    </button>
                  ))}
                </span>
              )}
            </span>
          </span>
        </div>
      )}
      {active && drafting && (
        <div className="copilot-row">
          <input
            autoFocus
            value={instruction}
            placeholder="¿Qué quieres decir? Ej.: ofrece una cita el jueves en la mañana"
            onChange={(e) => setInstruction(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                draft();
              } else if (e.key === "Escape") setDrafting(false);
            }}
          />
          <button type="button" className="primary" disabled={busy === "draft"} onClick={draft}>
            {busy === "draft" ? "Redactando…" : "Redactar"}
          </button>
        </div>
      )}
      {error && <div className="small" style={{ color: "var(--bad)" }}>{error}</div>}
    </div>
  );
}
