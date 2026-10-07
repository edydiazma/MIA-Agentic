"use client";

import type { CatalogInput, Condition, InputValue } from "@/lib/flow-types";
import { Field } from "@/components/ui";
import { hasRefOptions, useRefOptions } from "./refOptions";
import styles from "./flows.module.css";

const SIMPLE_OPS = [
  ["eq", "es igual a"],
  ["contains", "contiene"],
  ["gt", "es mayor que"],
  ["lt", "es menor que"],
] as const;

export const EXPRESSION_HINT = "Usa {{contact.name}}, {{vars.nombre}}, {{last_message.text}} o {{fields.clave}}";

function ListEditor({
  value,
  onChange,
  max,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  max?: number;
  placeholder?: string;
}) {
  const items = value.length ? value : [];
  return (
    <div className={styles.listEditor}>
      {items.map((item, i) => (
        <div key={i} className={styles.listRow}>
          <input
            value={item}
            placeholder={placeholder}
            onChange={(e) => onChange(items.map((x, j) => (j === i ? e.target.value : x)))}
          />
          <button type="button" className="icon" aria-label="Quitar" onClick={() => onChange(items.filter((_, j) => j !== i))}>
            ✕
          </button>
        </div>
      ))}
      {(!max || items.length < max) && (
        <button type="button" onClick={() => onChange([...items, ""])}>
          + Agregar
        </button>
      )}
    </div>
  );
}

/** Condición simple (modo Junior): izquierda · operador · derecha. Las compuestas se editan en Avanzado. */
function SimpleCondition({ value, onChange, readOnly }: { value: Condition | null; onChange: (c: Condition) => void; readOnly?: boolean }) {
  const simple = !value || ["eq", "gt", "lt", "contains"].includes(value.op);
  if (!simple) {
    return <p className="small muted">Condición compuesta: edítala en modo Avanzado.</p>;
  }
  const c = (value as Extract<Condition, { left: unknown }>) ?? { op: "eq", left: "", right: "" };
  const set = (patch: Partial<{ op: string; left: string; right: string }>) =>
    onChange({ op: (patch.op ?? c.op) as "eq", left: patch.left ?? (c.left as string) ?? "", right: patch.right ?? (c.right as string) ?? "" });
  return (
    <div className="stack" style={{ gap: 6 }}>
      <input disabled={readOnly} placeholder="{{last_message.text}}" value={(c.left as string) ?? ""} onChange={(e) => set({ left: e.target.value })} />
      <select disabled={readOnly} value={c.op} onChange={(e) => set({ op: e.target.value })}>
        {SIMPLE_OPS.map(([op, label]) => (
          <option key={op} value={op}>
            {label}
          </option>
        ))}
      </select>
      <input disabled={readOnly} placeholder="valor" value={(c.right as string) ?? ""} onChange={(e) => set({ right: e.target.value })} />
    </div>
  );
}

function RefSelect({ input, value, onChange, readOnly }: { input: CatalogInput; value: InputValue; onChange: (v: InputValue) => void; readOnly?: boolean }) {
  const options = useRefOptions(input.type);
  const current = value === null || value === undefined ? "" : String(value);
  if (options === null || options.length === 0) {
    return (
      <input
        disabled={readOnly}
        value={current}
        placeholder={options === null ? "Cargando…" : "Escribe el valor"}
        onChange={(e) => onChange(e.target.value || null)}
      />
    );
  }
  return (
    <select
      disabled={readOnly}
      value={current}
      onChange={(e) => {
        const opt = options.find((o) => String(o.value) === e.target.value);
        onChange(opt ? opt.value : null);
      }}
    >
      <option value="">{input.required ? "Selecciona…" : "— Ninguno —"}</option>
      {options.map((o) => (
        <option key={String(o.value)} value={String(o.value)}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function InputControl({
  input,
  value,
  onChange,
  readOnly,
}: {
  input: CatalogInput;
  value: InputValue;
  onChange: (v: InputValue) => void;
  readOnly?: boolean;
}) {
  if (input.type === "condition") return <SimpleCondition value={(value as Condition) ?? null} onChange={onChange} readOnly={readOnly} />;
  if (input.type === "buttons" || input.type === "options") {
    return (
      <ListEditor
        value={Array.isArray(value) ? (value as string[]) : []}
        onChange={(v) => !readOnly && onChange(v)}
        max={input.type === "buttons" ? 3 : undefined}
        placeholder={input.type === "buttons" ? "Texto del botón (máx. 20)" : "Opción"}
      />
    );
  }
  if (input.type === "select") {
    return (
      <select disabled={readOnly} value={value == null ? "" : String(value)} onChange={(e) => onChange(e.target.value || null)}>
        {!input.required && <option value="">—</option>}
        {(input.options ?? []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if (input.type === "number" || input.type === "duration") {
    return (
      <input
        type="number"
        min={0}
        disabled={readOnly}
        value={value == null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  }
  if (hasRefOptions(input.type)) return <RefSelect input={input} value={value} onChange={onChange} readOnly={readOnly} />;
  const long = input.name === "text" || input.name === "body" || input.name === "instruction" || input.name === "note";
  return long ? (
    <textarea rows={3} disabled={readOnly} value={value == null ? "" : String(value)} onChange={(e) => onChange(e.target.value || null)} />
  ) : (
    <input disabled={readOnly} value={value == null ? "" : String(value)} onChange={(e) => onChange(e.target.value || null)} />
  );
}

export default function BlockInputsForm({
  inputs,
  values,
  onChange,
  readOnly,
  errors,
}: {
  inputs: CatalogInput[];
  values: Record<string, InputValue>;
  onChange: (name: string, value: InputValue) => void;
  readOnly?: boolean;
  errors?: string[];
}) {
  if (!inputs.length) return <p className="small muted">Este bloque no necesita datos.</p>;
  return (
    <div className="form">
      {inputs.map((input) => (
        <Field
          key={input.name}
          label={`${input.label}${input.required ? " *" : ""}`}
          hint={input.type === "text" ? EXPRESSION_HINT : input.type === "duration" ? "En minutos" : undefined}
        >
          <InputControl input={input} value={values?.[input.name] ?? null} onChange={(v) => onChange(input.name, v)} readOnly={readOnly} />
        </Field>
      ))}
      {errors && errors.length > 0 && (
        <div className={styles.errors}>
          <ul>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
