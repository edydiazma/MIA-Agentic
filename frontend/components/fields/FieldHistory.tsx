"use client";

import Link from "next/link";
import { timeAgo, type FieldChange } from "@/lib/api";
import { Badge, Empty, ErrorBox, Loading, useApi } from "@/components/ui";

export function sourceBadge(c: Pick<FieldChange, "source" | "agent_name">) {
  if (c.source === "ai") return <Badge tone="info">🤖 IA</Badge>;
  if (c.source === "import") return <Badge>📄 Importación</Badge>;
  return <Badge tone="ok">👤 {c.agent_name ?? "Asesor"}</Badge>;
}

/** Historial de cambios de la ficha del cliente (quién, cuándo y de dónde vino). */
export default function FieldHistory({ contactId, reloadKey = 0 }: { contactId: number; reloadKey?: number }) {
  const history = useApi<FieldChange[]>(`/api/contacts/${contactId}/history?_=${reloadKey}`);
  if (history.loading && !history.data) return <Loading />;
  if (history.error) return <ErrorBox error={history.error} />;
  if (!history.data?.length) return <Empty>Sin cambios registrados.</Empty>;
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
      {history.data.map((h) => (
        <li key={h.id} className="small">
          <div className="inline" style={{ gap: 6 }}>
            <strong>{h.label}</strong>
            {sourceBadge(h)}
            <span className="muted">{timeAgo(h.created_at)}</span>
          </div>
          <div style={{ overflowWrap: "anywhere" }}>
            <span className="muted" style={{ textDecoration: h.old_value ? "line-through" : undefined }}>
              {h.old_value ?? "vacío"}
            </span>{" "}
            → {h.new_value ?? <span className="muted">vacío</span>}
          </div>
          {h.conversation_id && (
            <Link href={`/conversaciones?id=${h.conversation_id}`} className="small">
              Ver conversación
            </Link>
          )}
        </li>
      ))}
    </ul>
  );
}
