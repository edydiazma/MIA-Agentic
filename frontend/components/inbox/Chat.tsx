"use client";

import { useEffect, useRef, useState } from "react";
import {
  CHANNEL_ICONS,
  CHANNEL_LABELS,
  STATUS_LABEL,
  api,
  contactLabel,
  fmtTime,
  mediaUrl,
  send,
  type Agent,
  type Conversation,
  type Message,
} from "@/lib/api";
import type { QuickReplyV2 } from "@/lib/productivity-types";
import { useApi } from "@/components/ui";
import { CloseModal, ResourceModal, TemplateModal, TransferModal } from "./modals";
import ConversationTags from "./ConversationTags";
import CopilotBar, { type CopilotPick } from "@/components/copilot/CopilotBar";
import EmailMessage from "./EmailMessage";
import EmailComposer from "./EmailComposer";
import OutboundCallButton from "@/components/voice/OutboundCallButton";

const SENDER: Record<Message["sender_type"], string> = {
  contact: "",
  bot: "🤖 Bot",
  agent: "Asesor",
  campaign: "📣 Campaña",
  system: "",
};
const TICKS: Record<string, string> = { sent: "✓", delivered: "✓✓", read: "✓✓", failed: "⚠", pending: "…" };

function Media({ m }: { m: Message }) {
  if (!m.has_media) return m.type !== "text" && m.type !== "template" && !m.text ? <em className="muted">[{m.type}]</em> : null;
  const url = mediaUrl(m.id);
  if (m.type === "image" || m.type === "sticker")
    return (
      <a href={url} target="_blank" rel="noreferrer">
        <img src={url} alt="imagen" className={m.type} />
      </a>
    );
  if (m.type === "audio")
    return (
      <div>
        <audio controls src={url} />
        {m.transcript && <p className="transcript">“{m.transcript}”</p>}
      </div>
    );
  if (m.type === "video") return <video controls src={url} />;
  return (
    <a className="doc" href={url} target="_blank" rel="noreferrer">
      📄 {m.media_filename || "Documento"}
    </a>
  );
}

function Bubble({ m, agentName }: { m: Message; agentName: (id: number | null) => string | null }) {
  if (m.sender_type === "system")
    return (
      <div className="bubble system">
        {m.text} · {fmtTime(m.created_at)}
      </div>
    );
  const label =
    m.sender_type === "agent" ? agentName(m.sender_agent_id) ?? SENDER.agent : SENDER[m.sender_type];
  return (
    <div className={`bubble ${m.direction} ${m.sender_type}`}>
      {m.direction === "out" && (
        <div className="sender">
          {label}
          {m.template_name && <span> · plantilla «{m.template_name}»</span>}
        </div>
      )}
      <Media m={m} />
      {m.type === "email" ? <EmailMessage messageId={m.id} fallback={m.text} /> : m.text && <p>{m.text}</p>}
      {m.type === "audio" && !m.has_media && m.transcript && <p className="transcript">“{m.transcript}”</p>}
      <div className="meta">
        {fmtTime(m.created_at)}
        {m.direction === "out" && <span className={`tick ${m.status}`}> {TICKS[m.status] ?? ""}</span>}
      </div>
      {m.error && <div className="error small">{m.error}</div>}
    </div>
  );
}

type Props = {
  conversation: Conversation;
  messages: Message[];
  me: Agent | null;
  agentName: (id: number | null) => string | null;
  onMessage: (m: Message) => void;
  onConversation: (c: Conversation) => void;
  onBack: () => void;
};

type ModalKind = "transfer" | "close" | "template" | "resource" | null;

export default function Chat({ conversation: c, messages, me, agentName, onMessage, onConversation, onBack }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [sugIndex, setSugIndex] = useState(0);
  const bottom = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  // Respuestas rápidas disponibles para el grupo de esta conversación, con variables ya resueltas
  const quick = useApi<QuickReplyV2[]>(`/api/quick-replies?conversation_id=${c.id}`);
  const [picked, setPicked] = useState<QuickReplyV2 | null>(null);
  const [noteMode, setNoteMode] = useState(false);
  // Sugerencia del copiloto insertada en el compositor (al enviar se reporta si se usó tal cual o editada)
  const [copilotPick, setCopilotPick] = useState<CopilotPick | null>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, c.id]);
  useEffect(() => {
    setError(null);
    setText("");
    setPicked(null);
    setNoteMode(false);
    setCopilotPick(null);
  }, [c.id]);

  // Ventana de respuesta libre de un asesor: WhatsApp 24 h; Messenger e Instagram 7 días (HUMAN_AGENT); chat web siempre
  const isWhatsApp = c.channel_provider === "whatsapp_cloud";
  const windowMs = c.channel_provider === "webchat" ? Infinity : (isWhatsApp ? 24 : 7 * 24) * 3600 * 1000;
  const windowOpen =
    windowMs === Infinity || (!!c.last_inbound_at && Date.now() - new Date(c.last_inbound_at).getTime() < windowMs);

  const slash = text.startsWith("/") && !text.includes(" ") ? text.slice(1).toLowerCase() : null;
  const suggestions =
    slash === null
      ? []
      : (quick.data ?? [])
          .filter((q) =>
            [q.shortcut, q.title ?? "", q.category ?? ""].some((v) => v.toLowerCase().includes(slash)),
          )
          .slice(0, 8);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function sendText(e?: React.FormEvent) {
    e?.preventDefault();
    const body = text.trim();
    if (!body) return;
    await run(async () => {
      if (noteMode) {
        const r = await send<{ message: Message }>(`/api/conversations/${c.id}/notes`, "POST", { text: body });
        onMessage(r.message);
      } else if (picked) {
        // Respuesta rápida: envía el texto (editado o no) y sus adjuntos; cuenta el uso
        const r = await send<{ messages: Message[] }>(
          `/api/conversations/${c.id}/quick-replies/${picked.id}/send`, "POST", { text: body });
        r.messages.forEach(onMessage);
      } else {
        onMessage(await send<Message>(`/api/conversations/${c.id}/messages`, "POST", { text: body }));
        if (copilotPick) {
          // El servidor decide «aceptada» (sin cambios) o «editada» comparando con lo sugerido
          send(`/api/copilot/suggestions/${copilotPick.id}/outcome`, "POST", { status: "accepted", final_text: body })
            .catch(() => undefined);
        }
      }
      setText("");
      setPicked(null);
      setCopilotPick(null);
    });
  }

  async function attach(file: File) {
    const form = new FormData();
    form.append("file", file);
    if (text.trim()) form.append("caption", text.trim());
    await run(async () => {
      onMessage(await api<Message>(`/api/conversations/${c.id}/attachments`, { method: "POST", body: form }));
      setText("");
    });
  }

  const action = (path: string) =>
    run(async () => onConversation(await send<Conversation>(`/api/conversations/${c.id}/${path}`, "POST")));

  function pickSuggestion(q: QuickReplyV2) {
    setText(q.rendered);
    setPicked(q);
    setSugIndex(0);
  }

  return (
    <section className="chat">
      <header className="chat-head">
        <div className="inline" style={{ flexWrap: "nowrap", minWidth: 0 }}>
          <button className="icon" onClick={onBack} aria-label="Volver" title="Volver a la lista">
            ←
          </button>
          <div style={{ minWidth: 0 }}>
            <strong>{contactLabel(c.contact)}</strong>{" "}
            <span className="muted">
              {CHANNEL_ICONS[c.channel_provider]}{" "}
              {isWhatsApp
                ? c.contact.wa_id ? `+${c.contact.wa_id}` : c.contact.wa_username ? `@${c.contact.wa_username}` : "Usuario de WhatsApp"
                : `${CHANNEL_LABELS[c.channel_provider]}${c.channel_name ? ` · ${c.channel_name}` : ""}`}
            </span>
            <div className="small">
              <span className={`status ${c.status}`}>{STATUS_LABEL[c.status]}</span>
              {c.group && <span className="muted"> · {c.group.name}</span>}
              {c.assigned_agent && <span className="muted"> · {c.assigned_agent.name}</span>}
              {c.status === "human" && c.handoff_reason && <span className="muted"> · Motivo: {c.handoff_reason}</span>}
              {c.status === "closed" && c.typification && <span className="muted"> · {c.typification}</span>}
            </div>
            <ConversationTags conversation={c} onConversation={onConversation} />
          </div>
        </div>
        <div className="actions">
          {isWhatsApp && <OutboundCallButton conversationId={c.id} compact />}
          {(c.status !== "human" || c.assigned_agent?.id !== me?.id) && (
            <button disabled={busy} onClick={() => action("assign")}>
              Tomar
            </button>
          )}
          <button disabled={busy} onClick={() => setModal("transfer")}>
            Transferir
          </button>
          {c.status !== "bot" && (
            <button disabled={busy} onClick={() => action("release")}>
              Devolver al bot
            </button>
          )}
          {c.status !== "closed" && (
            <button disabled={busy} onClick={() => setModal("close")}>
              Cerrar
            </button>
          )}
        </div>
      </header>

      <div className="messages">
        {messages.map((m) => (
          <Bubble key={m.id} m={m} agentName={agentName} />
        ))}
        <div ref={bottom} />
      </div>

      {c.status !== "closed" && (
        <CopilotBar
          conversationId={c.id}
          active={c.status === "human" && windowOpen && !noteMode}
          text={text}
          setText={(t) => {
            setText(t);
            setPicked(null);
          }}
          onPick={setCopilotPick}
          onOpenTemplate={() => setModal("template")}
          onOpenTransfer={() => setModal("transfer")}
        />
      )}
      {error && <div className="error bar">{error}</div>}
      {!windowOpen ? (
        isWhatsApp ? (
          <div className="notice row">
            <span>Pasaron más de 24 h desde el último mensaje del cliente. WhatsApp solo permite plantillas aprobadas.</span>
            <button className="primary" onClick={() => setModal("template")}>
              Enviar plantilla
            </button>
          </div>
        ) : (
          <div className="notice row">
            <span>
              Pasaron más de 7 días desde el último mensaje del cliente: {CHANNEL_LABELS[c.channel_provider]} no permite
              escribirle hasta que vuelva a escribir.
            </span>
          </div>
        )
      ) : (
        <>
        {c.channel_provider === "email" && <EmailComposer conversationId={c.id} onSent={onMessage} />}
        <form className={`composer${noteMode ? " note" : ""}`} onSubmit={sendText}>
          {picked && picked.attachments.length > 0 && !noteMode && (
            <div className="small muted" style={{ width: "100%" }}>
              Se enviará con: {picked.attachments.map((a) => `📎 ${a.name}`).join(", ")}{" "}
              <button type="button" className="link small" onClick={() => setPicked(null)}>
                Quitar
              </button>
            </div>
          )}
          {suggestions.length > 0 && (
            <div className="suggestions" role="listbox">
              {suggestions.map((q, i) => (
                <button
                  type="button"
                  key={q.id}
                  className={i === sugIndex ? "active" : ""}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    pickSuggestion(q);
                  }}
                >
                  <span className="qr-pick">
                    {q.category && <span className="qr-cat">{q.category}</span>}
                    <span>
                      <strong>/{q.shortcut}</strong>
                      {q.title && <span> · {q.title}</span>}
                      {q.attachments.length > 0 && <span className="muted"> · 📎 {q.attachments.length}</span>}
                    </span>
                    <span className="muted">{q.rendered.slice(0, 120)}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
          <button type="button" title="Adjuntar archivo" disabled={busy} onClick={() => fileInput.current?.click()}>
            📎
          </button>
          <button type="button" title="Recursos" disabled={busy} onClick={() => setModal("resource")}>
            📁
          </button>
          {isWhatsApp && (
            <button type="button" title="Plantilla" disabled={busy} onClick={() => setModal("template")}>
              Plantilla
            </button>
          )}
          <button
            type="button"
            title="Nota interna: solo la ve tu equipo; escribe @nombre para avisarle a un compañero"
            aria-pressed={noteMode}
            className={noteMode ? "primary" : ""}
            onClick={() => setNoteMode((v) => !v)}
          >
            📝 Nota
          </button>
          <input
            ref={fileInput}
            type="file"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) attach(f);
              e.target.value = "";
            }}
          />
          <textarea
            value={text}
            rows={1}
            placeholder={
              noteMode
                ? "Nota interna (no se envía al cliente). @nombre avisa a un compañero"
                : c.status === "bot"
                  ? "Escribir aquí toma la conversación y pausa al bot… ( / para respuestas rápidas)"
                  : "Escribe un mensaje ( / para respuestas rápidas)"
            }
            onChange={(e) => {
              setText(e.target.value);
              setSugIndex(0);
              if (!e.target.value) setPicked(null);
            }}
            onKeyDown={(e) => {
              if (suggestions.length) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSugIndex((i) => (i + 1) % suggestions.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSugIndex((i) => (i - 1 + suggestions.length) % suggestions.length);
                  return;
                }
                if (e.key === "Enter" || e.key === "Tab") {
                  e.preventDefault();
                  pickSuggestion(suggestions[Math.min(sugIndex, suggestions.length - 1)]);
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendText();
              }
            }}
          />
          <button className="primary" disabled={busy || !text.trim()}>
            {noteMode ? "Guardar nota" : "Enviar"}
          </button>
        </form>
        </>
      )}

      {modal === "transfer" && (
        <TransferModal
          conversation={c}
          onClose={() => setModal(null)}
          onDone={(x) => {
            onConversation(x);
            setModal(null);
          }}
        />
      )}
      {modal === "close" && (
        <CloseModal
          conversation={c}
          onClose={() => setModal(null)}
          onDone={(x) => {
            onConversation(x);
            setModal(null);
          }}
        />
      )}
      {modal === "template" && (
        <TemplateModal
          conversation={c}
          onClose={() => setModal(null)}
          onSent={(m) => {
            onMessage(m);
            setModal(null);
          }}
        />
      )}
      {modal === "resource" && (
        <ResourceModal
          conversation={c}
          onClose={() => setModal(null)}
          onSent={(m) => {
            onMessage(m);
            setModal(null);
          }}
        />
      )}
    </section>
  );
}
