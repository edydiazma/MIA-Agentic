"use client";

import { useMemo } from "react";
import type { Template } from "@/lib/api";
import { Card, Field, Toggle, useApi } from "@/components/ui";
import type { SegmentField, Step } from "@/lib/journey-types";
import RuleBuilder, { toGroup } from "./RuleBuilder";

type Cfg = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];

function TemplatePicker({ value, onChange }: { value: Cfg; onChange: (v: Cfg) => void }) {
  const templates = useApi<Template[]>("/api/templates");
  const usable = useMemo(() => (templates.data ?? []).filter((t) => t.status === "APPROVED" && t.supported), [templates.data]);
  const key = value.template_name ? `${value.template_name}|${value.language}` : "";
  const tpl = usable.find((t) => `${t.name}|${t.language}` === key);
  const params: string[] = value.params ?? [];
  return (
    <>
      <Field label="Plantilla aprobada" hint={tpl ? `${tpl.category} · ${tpl.body}` : templates.error ?? undefined}>
        <select value={key} onChange={(e) => {
          const t = usable.find((x) => `${x.name}|${x.language}` === e.target.value);
          onChange({ ...value, template_name: t?.name ?? "", language: t?.language ?? "es", params: t ? t.variables.map(() => "") : [] });
        }}>
          <option value="">Selecciona…</option>
          {usable.map((t) => (
            <option key={`${t.name}|${t.language}`} value={`${t.name}|${t.language}`}>{t.name} ({t.language}) · {t.category}</option>
          ))}
        </select>
      </Field>
      {tpl?.variables.map((v, i) => (
        <Field key={v} label={`Variable {{${v}}}`} hint={i === 0 ? "Usa {{nombre}} para el nombre del cliente" : undefined}>
          <input value={params[i] ?? ""} onChange={(e) => onChange({ ...value, params: tpl.variables.map((_, j) => (j === i ? e.target.value : params[j] ?? "")) })} />
        </Field>
      ))}
    </>
  );
}

export default function StepDrawer({ step, fields, labels, onChange, onRemove }: {
  step: Step; fields: SegmentField[]; labels: Record<string, string>;
  onChange: (patch: Partial<Step>) => void; onRemove: () => void;
}) {
  const flows = useApi<{ id: number; name: string }[]>(step.type === "start_flow" ? "/api/flows" : null);
  const c: Cfg = step.config ?? {};
  const set = (patch: Cfg) => onChange({ config: { ...c, ...patch } });
  const num = (v: string) => (v === "" ? null : Number(v));

  let body: React.ReactNode = null;
  switch (step.type) {
    case "send_template":
      body = (
        <>
          <TemplatePicker value={c} onChange={(v) => onChange({ config: v })} />
          <Toggle checked={c.marketing !== false} onChange={(v) => set({ marketing: v })} label="Es marketing (respeta opt-out, consentimiento y límites)" />
        </>
      );
      break;
    case "send_text":
      body = (
        <>
          <Field label="Mensaje" hint="{{nombre}} = nombre · {{link:https://…}} = enlace con seguimiento de clics">
            <textarea rows={5} value={c.text ?? ""} onChange={(e) => set({ text: e.target.value })} />
          </Field>
          <Field label="Canal" hint="Automático: WhatsApp dentro de las 24 h; si no, correo; chat web solo si está conectado">
            <select value={c.channel ?? "auto"} onChange={(e) => set({ channel: e.target.value })}>
              <option value="auto">Automático (prioridad del journey)</option>
              <option value="whatsapp_cloud">WhatsApp</option>
              <option value="email">Correo</option>
              <option value="webchat">Chat web</option>
            </select>
          </Field>
          <p className="small muted">Plantilla de respaldo (fuera de la ventana de 24 h de WhatsApp):</p>
          <TemplatePicker value={c.fallback_template ?? {}} onChange={(v) => set({ fallback_template: v })} />
          <Toggle checked={c.marketing !== false} onChange={(v) => set({ marketing: v })} label="Es marketing" />
        </>
      );
      break;
    case "send_email":
      body = (
        <>
          <Field label="Asunto"><input value={c.subject ?? ""} onChange={(e) => set({ subject: e.target.value })} /></Field>
          <Field label="Cuerpo" hint="{{nombre}} y {{link:https://…}} disponibles">
            <textarea rows={8} value={c.body ?? ""} onChange={(e) => set({ body: e.target.value })} />
          </Field>
        </>
      );
      break;
    case "wait":
      body = (
        <div className="inline">
          {(["days", "hours", "minutes"] as const).map((k) => (
            <Field key={k} label={{ days: "Días", hours: "Horas", minutes: "Minutos" }[k]}>
              <input type="number" min={0} style={{ width: 80 }} value={c[k] ?? ""} onChange={(e) => set({ [k]: num(e.target.value) })} />
            </Field>
          ))}
        </div>
      );
      break;
    case "wait_until":
      body = (
        <>
          <Field label="Hora (zona de la empresa o del journey)"><input type="time" value={c.time ?? ""} onChange={(e) => set({ time: e.target.value })} /></Field>
          <Field label="Solo estos días (opcional)">
            <div className="chips">
              {WEEKDAYS.map((d, i) => {
                const on = (c.weekdays ?? []).includes(i);
                return (
                  <button key={d} className={`chip ${on ? "active" : ""}`} onClick={() =>
                    set({ weekdays: on ? c.weekdays.filter((x: number) => x !== i) : [...(c.weekdays ?? []), i] })}>{d}</button>
                );
              })}
            </div>
          </Field>
        </>
      );
      break;
    case "branch": {
      const cond = c.condition ?? { type: "replied", within_hours: 24 };
      body = (
        <>
          <Field label="Condición">
            <select value={cond.type} onChange={(e) => set({ condition: e.target.value === "field"
              ? { type: "field", rule: { all: [] } } : { type: e.target.value, within_hours: cond.within_hours ?? 24 } })}>
              <option value="replied">Respondió</option>
              <option value="read">Leyó el mensaje</option>
              <option value="clicked">Hizo clic en un enlace</option>
              <option value="field">Cumple una regla (datos del cliente)</option>
            </select>
          </Field>
          {cond.type === "field" ? (
            <RuleBuilder value={toGroup(cond.rule)} fields={fields} onChange={(g) => set({ condition: { ...cond, rule: g } })} />
          ) : (
            <Field label="Esperar hasta (horas)" hint="Si ocurre antes, sigue por «Sí» de inmediato">
              <input type="number" min={1} max={720} value={cond.within_hours ?? 24}
                onChange={(e) => set({ condition: { ...cond, within_hours: num(e.target.value) } })} />
            </Field>
          )}
        </>
      );
      break;
    }
    case "split": {
      const variants: { key: string; weight: number }[] = c.variants ?? [];
      const total = variants.reduce((a, v) => a + (Number(v.weight) || 0), 0);
      const setVariants = (vs: { key: string; weight: number }[]) => {
        const branches = Object.fromEntries(vs.map((v) => [v.key, step.branches?.[v.key] ?? null]));
        onChange({ config: { ...c, variants: vs }, branches });
      };
      body = (
        <>
          {variants.map((v, i) => (
            <div key={i} className="inline">
              <input style={{ width: 70 }} value={v.key} onChange={(e) =>
                setVariants(variants.map((x, j) => (j === i ? { ...x, key: e.target.value.replace(/[^A-Za-z0-9_-]/g, "") } : x)))} />
              <input type="number" min={0} max={100} style={{ width: 80 }} value={v.weight} onChange={(e) =>
                setVariants(variants.map((x, j) => (j === i ? { ...x, weight: Number(e.target.value) } : x)))} />
              <span className="muted">%</span>
              {variants.length > 2 && <button className="icon" onClick={() => setVariants(variants.filter((_, j) => j !== i))}>✕</button>}
            </div>
          ))}
          <div className="inline">
            <button onClick={() => setVariants([...variants, { key: String.fromCharCode(65 + variants.length), weight: 0 }])}>+ Variante</button>
            <span className={total === 100 ? "muted small" : "small"} style={total === 100 ? undefined : { color: "var(--bad)" }}>Total {total}%</span>
          </div>
          <p className="small muted">Cada cliente cae siempre en la misma variante.</p>
        </>
      );
      break;
    }
    case "update_contact":
      body = (
        <>
          <Field label="Campo" hint="stage, name, email, notes o custom:<clave>">
            <input value={c.field ?? ""} onChange={(e) => set({ field: e.target.value })} list="journey-fields" />
            <datalist id="journey-fields">
              {["stage", "name", "email", "notes"].map((f) => <option key={f} value={f} />)}
            </datalist>
          </Field>
          <Field label="Valor"><input value={c.value ?? ""} onChange={(e) => set({ value: e.target.value })} /></Field>
        </>
      );
      break;
    case "add_tag":
      body = (
        <Field label="Etiquetas" hint="Separadas por coma">
          <input value={(c.tags ?? []).join(", ")} onChange={(e) => set({ tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })} />
        </Field>
      );
      break;
    case "create_deal":
    case "move_stage":
      body = (
        <>
          <Field label="Línea de negocio (pipeline)"><input value={c.pipeline ?? ""} onChange={(e) => set({ pipeline: e.target.value })} /></Field>
          {step.type === "create_deal" && (
            <>
              <Field label="Nombre"><input value={c.name ?? ""} onChange={(e) => set({ name: e.target.value })} /></Field>
              <Field label="Monto"><input type="number" value={c.amount ?? ""} onChange={(e) => set({ amount: num(e.target.value) })} /></Field>
            </>
          )}
          <Field label="Etapa"><input value={c.stage ?? ""} onChange={(e) => set({ stage: e.target.value })} /></Field>
        </>
      );
      break;
    case "notify_agent":
      body = (
        <>
          <Field label="A quién">
            <select value={c.to ?? "owner"} onChange={(e) => set({ to: e.target.value })}>
              <option value="owner">Dueño del cliente</option>
              <option value="last_agent">Último asesor que lo atendió</option>
              <option value="agent">Un asesor específico</option>
            </select>
          </Field>
          {c.to === "agent" && (
            <Field label="ID del asesor"><input type="number" value={c.agent_id ?? ""} onChange={(e) => set({ agent_id: num(e.target.value) })} /></Field>
          )}
          <Field label="Título"><input value={c.title ?? ""} onChange={(e) => set({ title: e.target.value })} /></Field>
          <Field label="Detalle"><input value={c.body ?? ""} onChange={(e) => set({ body: e.target.value })} /></Field>
        </>
      );
      break;
    case "start_flow":
      body = (
        <Field label="Flujo publicado">
          <select value={c.flow_id ?? ""} onChange={(e) => set({ flow_id: num(e.target.value) })}>
            <option value="">Selecciona…</option>
            {flows.data?.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
          </select>
        </Field>
      );
      break;
    case "exit":
      body = <Field label="Motivo (aparece en el reporte)"><input value={c.reason ?? ""} onChange={(e) => set({ reason: e.target.value })} /></Field>;
      break;
  }

  return (
    <Card title={`${labels[step.type] ?? step.type} · ${step.id}`} actions={<button className="danger" onClick={onRemove}>Quitar paso</button>}>
      <div className="stack">{body}</div>
    </Card>
  );
}
