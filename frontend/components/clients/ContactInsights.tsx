"use client";

import { useState } from "react";
import { fmtNum, type Contact } from "@/lib/api";
import { useApi } from "@/components/ui";
import { fmtDays, relDate, type ContactRow } from "@/lib/customer-types";

/** Copia al portapapeles con confirmación breve. */
function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="link small"
      aria-label={`Copiar ${label}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          /* sin permiso de portapapeles */
        }
      }}
    >
      {done ? "Copiado" : "Copiar"}
    </button>
  );
}

/** Teléfono, @usuario y BSUID de WhatsApp (docs/data-model.md §14). */
export function WhatsAppIdentity({ contact }: { contact: ContactRow }) {
  if (!contact.wa_id && !contact.wa_username && !contact.wa_bsuid) return null;
  return (
    <dl className="small" style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 8px", margin: "0 0 8px" }}>
      <dt className="muted">Teléfono</dt>
      <dd style={{ margin: 0 }}>
        {contact.wa_id ? `+${contact.wa_id}` : <span className="muted">Sin teléfono (usuario de WhatsApp)</span>}
      </dd>
      {contact.wa_username && (
        <>
          <dt className="muted">Usuario</dt>
          <dd style={{ margin: 0 }}>@{contact.wa_username}</dd>
        </>
      )}
      {contact.wa_bsuid && (
        <>
          <dt className="muted" title="Identificador de WhatsApp para tu empresa (Business-Scoped User ID)">
            BSUID
          </dt>
          <dd style={{ margin: 0, display: "flex", gap: 6, alignItems: "center", minWidth: 0 }}>
            <code style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={contact.wa_bsuid}>
              {contact.wa_bsuid}
            </code>
            <CopyButton value={contact.wa_bsuid} label="BSUID" />
          </dd>
        </>
      )}
    </dl>
  );
}

function When({ iso }: { iso: string | null | undefined }) {
  const d = relDate(iso);
  return <span title={d.title}>{d.text}</span>;
}

/** Primera/última interacción, días y volumen; consulta la ficha si el contacto llega sin las métricas. */
export default function ContactInsights({ contact }: { contact: Contact }) {
  const row = contact as ContactRow;
  const hasMetrics = row.first_interaction_at !== undefined;
  const full = useApi<ContactRow>(hasMetrics ? null : `/api/contacts/${contact.id}`);
  const c: ContactRow = hasMetrics ? row : { ...row, ...(full.data ?? {}) };
  if (c.first_interaction_at === undefined) return null;

  const items: [string, React.ReactNode][] = [
    ["Primera interacción", <When key="f" iso={c.first_interaction_at} />],
    ["Última interacción", <When key="l" iso={c.last_interaction_at} />],
    ["Último mensaje del cliente", <When key="i" iso={c.last_inbound_at} />],
    ["Días de vida", fmtDays(c.lifetime_days)],
    ["Días sin interacción", fmtDays(c.days_since_last_interaction)],
    ["Conversaciones", fmtNum(c.conversations_count ?? 0)],
    ["Mensajes", `${fmtNum(c.messages_in ?? 0)} recibidos · ${fmtNum(c.messages_out ?? 0)} enviados`],
  ];
  if (c.last_flow?.name) items.push(["Último flujo", <span key="fl">{c.last_flow.name} <When iso={c.last_flow_at} /></span>]);
  if (c.last_agent?.name) items.push(["Último agente", c.last_agent.name]);
  if (c.last_typification?.name) items.push(["Última tipificación", c.last_typification.name]);

  return (
    <div>
      <h3>Interacción</h3>
      <dl className="small" style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 8px", margin: 0 }}>
        {items.map(([label, value]) => (
          <div key={label} style={{ display: "contents" }}>
            <dt className="muted">{label}</dt>
            <dd style={{ margin: 0 }}>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
