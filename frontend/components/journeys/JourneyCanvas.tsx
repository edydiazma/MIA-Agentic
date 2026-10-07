"use client";

import { useEffect, useRef, useState } from "react";
import type { Definition, JourneyReport, Step, StepType } from "@/lib/journey-types";
import { isFork } from "./graph";
import css from "./journeys.module.css";

const MENU: [string, StepType[]][] = [
  ["Mensajes", ["send_template", "send_text", "send_email"]],
  ["Tiempo", ["wait", "wait_until"]],
  ["Lógica", ["branch", "split", "exit"]],
  ["Acciones", ["update_contact", "add_tag", "create_deal", "move_stage", "notify_agent", "start_flow"]],
];
const ICON: Record<StepType, string> = {
  send_template: "💬", send_text: "✉️", send_email: "📧", wait: "⏳", wait_until: "🕙", branch: "🔀", split: "🧪",
  update_contact: "✏️", add_tag: "🏷️", create_deal: "💼", move_stage: "➡️", notify_agent: "🔔", start_flow: "🤖", exit: "⏹",
};
const BRANCH_LABEL: Record<string, string> = { yes: "Sí", no: "No" };

export function summary(s: Step): string {
  const c = s.config ?? {};
  switch (s.type) {
    case "send_template": return c.template_name ? `${c.template_name} (${c.language})` : "Elige la plantilla";
    case "send_text": return c.text || "Escribe el mensaje";
    case "send_email": return c.subject || "Asunto del correo";
    case "wait": return [c.days && `${c.days} d`, c.hours && `${c.hours} h`, c.minutes && `${c.minutes} min`].filter(Boolean).join(" ") || "—";
    case "wait_until": return `a las ${c.time ?? "—"}`;
    case "branch": {
      const cond = c.condition ?? {};
      const what = { replied: "¿Respondió", read: "¿Leyó", clicked: "¿Hizo clic", field: "¿Cumple la regla" }[cond.type as string] ?? "¿…";
      return cond.type === "field" ? `${what}?` : `${what} en ${cond.within_hours ?? 24} h?`;
    }
    case "split": return (c.variants ?? []).map((v: { key: string; weight: number }) => `${v.key} ${v.weight}%`).join(" · ");
    case "update_contact": return `${c.field} = ${c.value ?? ""}`;
    case "add_tag": return (c.tags ?? []).join(", ") || "Etiquetas";
    case "create_deal": return `${c.pipeline} · ${c.name ?? ""}`;
    case "move_stage": return `${c.pipeline} → ${c.stage || "…"}`;
    case "notify_agent": return c.title ?? "";
    case "start_flow": return c.flow_id ? `Flujo #${c.flow_id}` : "Elige el flujo";
    case "exit": return c.reason ?? "Termina el journey";
  }
}

function AddMenu({ labels, onPick }: { labels: Record<string, string>; onPick: (t: StepType) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className={css.add} ref={ref}>
      <button className={css.addBtn} onClick={() => setOpen(!open)} aria-label="Agregar paso" title="Agregar paso">+</button>
      {open && (
        <div className={css.menu}>
          {MENU.map(([group, types]) => (
            <div key={group}>
              <div className={css.menuGroup}>{group}</div>
              {types.map((t) => (
                <button key={t} onClick={() => { setOpen(false); onPick(t); }}>{ICON[t]} {labels[t] ?? t}</button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function JourneyCanvas({
  definition, labels, entryLabel, selected, errors, report, readOnly, onSelect, onInsert,
}: {
  definition: Definition;
  labels: Record<string, string>;
  entryLabel: string;
  selected: string | null;
  errors: Record<string, string[]>;
  report?: JourneyReport | null;
  readOnly?: boolean;
  onSelect: (id: string) => void;
  onInsert: (parent: string | null, branch: string | null, type: StepType) => void;
}) {
  const by = new Map(definition.steps.map((s) => [s.id, s]));
  const stats = new Map((report?.steps ?? []).map((s) => [s.id, s]));
  const drawn = new Set<string>();

  const add = (parent: string | null, branch: string | null) =>
    readOnly ? null : <AddMenu labels={labels} onPick={(t) => onInsert(parent, branch, t)} />;

  function chain(id: string | null | undefined): React.ReactElement {
    if (!id || !by.has(id)) return <div className={css.end}>Fin</div>;
    if (drawn.has(id)) return <div className={css.jump}>↪ continúa en {id}</div>;
    drawn.add(id);
    const s = by.get(id)!;
    const errs = errors[id] ?? [];
    const st = stats.get(id);
    return (
      <div className={css.column}>
        <button
          className={`${css.card} ${selected === id ? css.selected : ""} ${errs.length ? css.invalid : ""}`}
          onClick={() => onSelect(id)}
        >
          <span className={css.cardHead}><span>{ICON[s.type]} {labels[s.type] ?? s.type}</span><code>{s.id}</code></span>
          <span className={css.cardBody}>{summary(s)}</span>
          {st && (st.sent > 0 || st.branched > 0) && (
            <span className={css.stats}>
              {st.sent > 0 && <span>{st.sent} enviados</span>}
              {st.read > 0 && <span>{st.read} leídos</span>}
              {st.replied > 0 && <span>{st.replied} resp.</span>}
              {st.clicked > 0 && <span>{st.clicked} clics</span>}
            </span>
          )}
          {errs.map((e) => <span key={e} className={css.cardErr}>⚠ {e}</span>)}
        </button>
        <div className={css.line} />
        {isFork(s) ? (
          <div className={css.branches}>
            {Object.entries(s.branches ?? {}).map(([k, target]) => (
              <div key={k} className={css.column}>
                <span className={css.branchLabel}>{BRANCH_LABEL[k] ?? `Variante ${k}`}</span>
                <div className={css.line} />
                {add(id, k)}
                <div className={css.line} />
                {chain(target)}
              </div>
            ))}
          </div>
        ) : s.type === "exit" ? (
          <div className={css.end}>Salida</div>
        ) : (
          <>
            {add(id, null)}
            <div className={css.line} />
            {chain(s.next)}
          </>
        )}
      </div>
    );
  }

  return (
    <div className={css.canvas}>
      <div className={css.column}>
        <span className={css.entry}>▶ {entryLabel}</span>
        <div className={css.line} />
        {add(null, null)}
        <div className={css.line} />
        {chain(definition.start)}
      </div>
    </div>
  );
}
