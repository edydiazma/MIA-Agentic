"use client";

import {
  CHANNEL_ICONS,
  CHANNEL_LABELS,
  STATUS_LABEL,
  contactLabel,
  type AgentDetail,
  type ChannelProvider,
  type Conversation,
  type Group,
} from "@/lib/api";

export type Filter = "open" | "unassigned" | "human" | "bot" | "mine" | "closed";

export const FILTERS: [Filter, string][] = [
  ["open", "Abiertas"],
  ["unassigned", "Sin asignar"],
  ["human", "Con asesor"],
  ["bot", "Con bot"],
  ["mine", "Mías"],
  ["closed", "Cerradas"],
];

function time(iso: string) {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("es", { day: "2-digit", month: "short" });
}

type Props = {
  conversations: Conversation[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  filter: Filter;
  onFilter: (f: Filter) => void;
  query: string;
  onQuery: (q: string) => void;
  groups: Group[];
  groupId: number | null;
  onGroup: (id: number | null) => void;
  isAdmin: boolean;
  agents: AgentDetail[];
  actAs: number | null;
  onActAs: (id: number | null) => void;
  channel: ChannelProvider | null;
  onChannel: (c: ChannelProvider | null) => void;
};

export default function ConversationList(p: Props) {
  const actingAs = p.agents.find((a) => a.id === p.actAs);
  return (
    <aside className="list">
      <div className="list-tools">
        <input placeholder="Buscar nombre, teléfono o @usuario" value={p.query} onChange={(e) => p.onQuery(e.target.value)} />
        <div className="chips">
          {FILTERS.map(([f, label]) => (
            <button key={f} className={p.filter === f ? "chip active" : "chip"} onClick={() => p.onFilter(f)}>
              {label}
            </button>
          ))}
        </div>
        <div className="inline" style={{ flexWrap: "nowrap" }}>
          <select
            value={p.groupId ?? ""}
            onChange={(e) => p.onGroup(e.target.value ? Number(e.target.value) : null)}
            aria-label="Grupo"
          >
            <option value="">Todos los grupos</option>
            {p.groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
          <select
            value={p.channel ?? ""}
            onChange={(e) => p.onChannel((e.target.value || null) as ChannelProvider | null)}
            aria-label="Canal"
          >
            <option value="">Todos los canales</option>
            {(Object.keys(CHANNEL_LABELS) as ChannelProvider[]).map((k) => (
              <option key={k} value={k}>
                {CHANNEL_LABELS[k]}
              </option>
            ))}
          </select>
          {p.isAdmin && (
            <select
              value={p.actAs ?? ""}
              onChange={(e) => p.onActAs(e.target.value ? Number(e.target.value) : null)}
              aria-label="Actuar como agente"
              title="Selecciona un agente para ver las conversaciones que está atendiendo"
            >
              <option value="">Actuar como agente…</option>
              {p.agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.online ? " ●" : ""}
                </option>
              ))}
            </select>
          )}
        </div>
        {actingAs && (
          <div className="row small" style={{ background: "var(--info-soft)", color: "var(--info)", padding: "4px 8px", borderRadius: 6 }}>
            <span>
              Viendo como: <strong>{actingAs.name}</strong>
            </span>
            <button className="link small" onClick={() => p.onActAs(null)}>
              Salir
            </button>
          </div>
        )}
      </div>
      <ul>
        {p.conversations.map((c) => (
          <li key={c.id} className={c.id === p.selectedId ? "selected" : ""} onClick={() => p.onSelect(c.id)}>
            <div className="row">
              <span className="name preview">
                <span title={c.channel_name ?? CHANNEL_LABELS[c.channel_provider]} aria-label={CHANNEL_LABELS[c.channel_provider]}>
                  {CHANNEL_ICONS[c.channel_provider] ?? ""}
                </span>{" "}
                {contactLabel(c.contact)}
              </span>
              <span className="muted small nowrap">{time(c.last_message_at)}</span>
            </div>
            <div className="row">
              <span className="preview">{c.last_message_preview}</span>
              {c.unread_count > 0 && <span className="badge">{c.unread_count}</span>}
            </div>
            <div className="row" style={{ justifyContent: "flex-start", flexWrap: "wrap" }}>
              <span className={`status ${c.status}`}>{STATUS_LABEL[c.status]}</span>
              {c.ad_source_type && <span className="small" title={c.ad_headline ?? undefined}>📣 Anuncio</span>}
              {c.group && <span className="tag">{c.group.name}</span>}
              {c.assigned_agent ? (
                <span className="muted small">{c.assigned_agent.name}</span>
              ) : (
                c.status === "human" && <span className="small" style={{ color: "var(--warn)" }}>Sin asignar</span>
              )}
            </div>
          </li>
        ))}
        {p.conversations.length === 0 && <li className="muted none">Sin conversaciones</li>}
      </ul>
    </aside>
  );
}
