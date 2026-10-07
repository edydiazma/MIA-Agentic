"use client";

import { useState } from "react";
import { send, type Conversation, type ConversationTag } from "@/lib/api";
import { useAction, useApi } from "@/components/ui";

/** Etiquetas de la conversación (las pone la IA o el asesor), editables en la cabecera del chat. */
export default function ConversationTags({
  conversation: c,
  onConversation,
}: {
  conversation: Conversation;
  onConversation: (c: Conversation) => void;
}) {
  const catalog = useApi<ConversationTag[]>("/api/conversation-tags");
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState("");
  const [run, busy, error] = useAction();
  const tags = c.tags ?? [];

  async function save(next: string[]) {
    const updated = await run(() => send<Conversation>(`/api/conversations/${c.id}/tags`, "PUT", { tags: next }));
    if (updated) onConversation(updated);
  }

  async function add(e?: React.FormEvent) {
    e?.preventDefault();
    const t = value.trim().toLowerCase();
    if (!t) return setAdding(false);
    if (!tags.includes(t)) await save([...tags, t]);
    setValue("");
    setAdding(false);
  }

  const listId = `conv-tags-${c.id}`;
  return (
    <div className="inline" style={{ gap: 4, marginTop: 4 }}>
      {tags.map((t) => (
        <span key={t} className="tag" style={{ margin: 0 }}>
          {t}
          <button
            className="link"
            style={{ marginLeft: 4, color: "inherit" }}
            disabled={busy}
            onClick={() => save(tags.filter((x) => x !== t))}
            aria-label={`Quitar etiqueta ${t}`}
          >
            ×
          </button>
        </span>
      ))}
      {adding ? (
        <form onSubmit={add} className="inline" style={{ gap: 4 }}>
          <input
            autoFocus
            list={listId}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => add()}
            onKeyDown={(e) => e.key === "Escape" && setAdding(false)}
            placeholder="etiqueta"
            style={{ width: 130, padding: "2px 6px", fontSize: 12 }}
            aria-label="Nueva etiqueta"
          />
          <datalist id={listId}>
            {(catalog.data ?? [])
              .filter((t) => !tags.includes(t.name))
              .map((t) => (
                <option key={t.name} value={t.name}>
                  {t.description}
                </option>
              ))}
          </datalist>
        </form>
      ) : (
        <button className="link small" onClick={() => setAdding(true)} disabled={busy}>
          + etiqueta
        </button>
      )}
      {error && <span className="error small">{error}</span>}
    </div>
  );
}
