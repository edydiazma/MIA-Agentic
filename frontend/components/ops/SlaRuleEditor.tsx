"use client";

import type { Group } from "@/lib/api";
import { Field, Toggle } from "@/components/ui";

export type SlaAction =
  | { type: "send_message"; text: string }
  | { type: "reassign_in_group" }
  | { type: "notify_supervisor" }
  | { type: "handoff"; group_id?: number | null }
  | { type: "typify"; name: string }
  | { type: "close"; typification?: string | null };

export const SLA_TYPES = ["sla_agent_no_reply", "sla_client_no_reply", "sla_unassigned"] as const;

export const ACTION_LABELS: Record<SlaAction["type"], string> = {
  send_message: "Enviar mensaje al cliente",
  reassign_in_group: "Reasignar a otro asesor del grupo",
  notify_supervisor: "Avisar al supervisor",
  handoff: "Transferir a asesor (grupo)",
  typify: "Tipificar",
  close: "Cerrar la conversación",
};

/** Acciones que tienen sentido para cada tipo de regla. */
const ALLOWED: Record<string, SlaAction["type"][]> = {
  sla_agent_no_reply: ["send_message", "reassign_in_group", "notify_supervisor", "typify", "close"],
  sla_client_no_reply: ["send_message", "handoff", "typify", "close", "notify_supervisor"],
  sla_unassigned: ["send_message", "reassign_in_group", "notify_supervisor", "typify", "close"],
};

export function emptySlaConfig(type: string): Record<string, any> {
  const first: Record<string, SlaAction> = {
    sla_agent_no_reply: { type: "notify_supervisor" },
    sla_client_no_reply: { type: "send_message", text: "¿Sigues ahí? Cuéntanos si podemos ayudarte en algo más." },
    sla_unassigned: { type: "reassign_in_group" },
  };
  return { minutes: type === "sla_client_no_reply" ? 30 : 5, group_ids: [], only_business_hours: true, actions: [first[type]] };
}

export function slaSummary(type: string, c: Record<string, any>, groups: Group[]): string {
  const who = { sla_agent_no_reply: "El asesor no responde", sla_client_no_reply: "El cliente no responde al bot",
                sla_unassigned: "Sin asesor asignado" }[type] ?? type;
  const gs = (c.group_ids ?? []).map((id: number) => groups.find((g) => g.id === id)?.name).filter(Boolean);
  const acts = (c.actions ?? []).map((a: SlaAction) => ACTION_LABELS[a.type]?.toLowerCase()).join(" + ");
  return `${who} en ${c.minutes} min${gs.length ? ` (${gs.join(", ")})` : ""} → ${acts || "sin acciones"}${c.only_business_hours ? " · solo en horario" : ""}`;
}

export default function SlaRuleEditor({ type, config, onChange, groups }: {
  type: string;
  config: Record<string, any>;
  onChange: (c: Record<string, any>) => void;
  groups: Group[];
}) {
  const actions: SlaAction[] = config.actions ?? [];
  const set = (k: string, v: any) => onChange({ ...config, [k]: v });
  const setAction = (i: number, a: SlaAction) => set("actions", actions.map((x, j) => (j === i ? a : x)));
  const allowed = ALLOWED[type] ?? Object.keys(ACTION_LABELS);
  const groupIds: number[] = config.group_ids ?? [];

  return (
    <div className="stack">
      <Field
        label="Minutos"
        hint={
          type === "sla_agent_no_reply" ? "Desde el último mensaje del cliente sin respuesta del asesor asignado."
            : type === "sla_client_no_reply" ? "Desde el último mensaje del bot sin respuesta del cliente."
            : "Desde la transferencia, mientras nadie la tome."
        }
      >
        <input type="number" min={1} value={config.minutes ?? 5} onChange={(e) => set("minutes", Number(e.target.value))} />
      </Field>
      <Field label="Grupos" hint="Vacío = todas las conversaciones.">
        <div className="inline" style={{ flexWrap: "wrap" }}>
          {groups.map((g) => (
            <label key={g.id} className="inline small">
              <input
                type="checkbox"
                checked={groupIds.includes(g.id)}
                onChange={(e) => set("group_ids", e.target.checked ? [...groupIds, g.id] : groupIds.filter((x) => x !== g.id))}
              />
              {g.name}
            </label>
          ))}
          {!groups.length && <span className="muted small">No hay grupos.</span>}
        </div>
      </Field>
      <Toggle
        checked={!!config.only_business_hours}
        onChange={(v) => set("only_business_hours", v)}
        label="Solo dentro del horario de atención"
      />
      <Field label="Acciones" hint="Se ejecutan en orden, una sola vez por ciclo (un nuevo mensaje del cliente inicia otro ciclo).">
        <div className="stack" style={{ gap: 8 }}>
          {actions.map((a, i) => (
            <div key={i} className="inline" style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
              <select
                aria-label={`Acción ${i + 1}`}
                value={a.type}
                onChange={(e) => {
                  const t = e.target.value as SlaAction["type"];
                  setAction(i, t === "send_message" ? { type: t, text: "" } : t === "typify" ? { type: t, name: "" } : ({ type: t } as SlaAction));
                }}
              >
                {allowed.map((t) => <option key={t} value={t}>{ACTION_LABELS[t]}</option>)}
              </select>
              {a.type === "send_message" && (
                <textarea
                  aria-label="Mensaje"
                  rows={2}
                  style={{ flex: 1, minWidth: 220 }}
                  value={a.text}
                  onChange={(e) => setAction(i, { ...a, text: e.target.value })}
                />
              )}
              {a.type === "typify" && (
                <input aria-label="Tipificación" placeholder="Tipificación" value={a.name}
                  onChange={(e) => setAction(i, { ...a, name: e.target.value })} />
              )}
              {a.type === "close" && (
                <input aria-label="Tipificación al cerrar" placeholder="Tipificación (opcional)" value={a.typification ?? ""}
                  onChange={(e) => setAction(i, { ...a, typification: e.target.value || null })} />
              )}
              {a.type === "handoff" && (
                <select aria-label="Grupo" value={a.group_id ?? ""}
                  onChange={(e) => setAction(i, { ...a, group_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">Enrutar con IA</option>
                  {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
              )}
              <button className="link small" onClick={() => set("actions", actions.filter((_, j) => j !== i))}>Quitar</button>
            </div>
          ))}
          <div>
            <button className="small" onClick={() => set("actions", [...actions, { type: allowed[0] } as SlaAction])}>
              + Agregar acción
            </button>
          </div>
        </div>
      </Field>
    </div>
  );
}
