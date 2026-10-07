"use client";

import { useState } from "react";
import { send, type Message } from "@/lib/api";
import { ErrorBox } from "@/components/ui";

/** Respuesta por correo con asunto y CC. Responde en el mismo hilo (In-Reply-To / References) y agrega la firma
 * del canal. Uso: en Chat.tsx, cuando `c.channel?.provider === "email"`, en lugar del compositor de texto:
 * `<EmailComposer conversationId={c.id} onSent={onMessage} />` (el envío normal de texto también responde el hilo). */
export default function EmailComposer({
  conversationId,
  defaultSubject,
  onSent,
}: {
  conversationId: number;
  defaultSubject?: string | null;
  onSent: (m: Message) => void;
}) {
  const [subject, setSubject] = useState("");
  const [cc, setCc] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const m = await send<Message>(`/api/conversations/${conversationId}/email`, "POST", {
        text: text.trim(),
        subject: subject.trim() || null,
        cc: cc.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean),
      });
      onSent(m);
      setText("");
      setCc("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo enviar el correo");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="email-composer" onSubmit={submit} style={{ display: "grid", gap: 6, padding: 8, borderTop: "1px solid var(--border)" }}>
      <ErrorBox error={error} />
      <div className="inline" style={{ gap: 6 }}>
        <input aria-label="Asunto" placeholder={defaultSubject ? `Asunto (vacío = Re: ${defaultSubject})` : "Asunto (vacío = responder el hilo)"}
          value={subject} onChange={(e) => setSubject(e.target.value)} style={{ flex: 2 }} maxLength={500} />
        <input aria-label="CC" placeholder="CC (separados por coma)" value={cc} onChange={(e) => setCc(e.target.value)} style={{ flex: 1 }} />
      </div>
      <textarea aria-label="Mensaje" rows={4} placeholder="Escribe tu respuesta…" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e as unknown as React.FormEvent);
        }} />
      <div className="inline" style={{ justifyContent: "space-between" }}>
        <span className="muted small">Se envía con la firma del buzón · ⌘/Ctrl + Enter</span>
        <button className="primary" type="submit" disabled={busy || !text.trim()}>
          {busy ? "Enviando…" : "Enviar correo"}
        </button>
      </div>
    </form>
  );
}
