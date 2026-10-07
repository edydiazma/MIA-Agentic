"use client";

import { OP_LABELS, type RuleLeaf, type RuleNode, type SegmentField } from "@/lib/journey-types";

type Group = { all: RuleNode[] } | { any: RuleNode[] };

const isGroup = (n: RuleNode): n is Group => "all" in n || "any" in n;
const isNot = (n: RuleNode): n is { not: RuleNode } => "not" in n;
const items = (g: Group) => ("all" in g ? g.all : g.any);
const withItems = (g: Group, list: RuleNode[]): Group => ("all" in g ? { all: list } : { any: list });

export function emptyRule(): Group {
  return { all: [] };
}

/** Normaliza lo guardado (o un {} vacío) a un grupo editable. */
export function toGroup(def: unknown): Group {
  const n = def as RuleNode | undefined;
  if (!n || typeof n !== "object" || Object.keys(n).length === 0) return emptyRule();
  return isGroup(n) ? n : { all: [n] };
}

function defaultValue(f: SegmentField | undefined, op: string): unknown {
  if (!f || op === "exists") return undefined;
  if (op === "between") return ["", ""];
  if (op === "in") return [];
  if (op === "within_days" || op === "before_days") return 30;
  if (f.type === "bool") return true;
  if (f.options?.length) return f.options[0];
  return "";
}

function ValueInput({ field, leaf, onChange }: { field?: SegmentField; leaf: RuleLeaf; onChange: (v: unknown) => void }) {
  const op = leaf.op;
  if (!field || op === "exists") return null;
  const inputType = field.type === "number" ? "number" : field.type === "date" ? "date" : field.type === "ts" ? "date" : "text";
  const cast = (v: string) => (field.type === "number" && v !== "" ? Number(v) : v);
  if (op === "within_days" || op === "before_days")
    return (
      <input type="number" min={0} max={3650} style={{ width: 90 }} value={Number(leaf.value ?? 0)}
        onChange={(e) => onChange(Number(e.target.value))} />
    );
  if (op === "between") {
    const [a, b] = (leaf.value as unknown[]) ?? ["", ""];
    return (
      <span className="inline">
        <input type={inputType} value={String(a ?? "")} onChange={(e) => onChange([cast(e.target.value), b])} />
        <span className="muted">y</span>
        <input type={inputType} value={String(b ?? "")} onChange={(e) => onChange([a, cast(e.target.value)])} />
      </span>
    );
  }
  if (op === "in") {
    const list = (leaf.value as unknown[]) ?? [];
    if (field.options?.length)
      return (
        <select multiple value={list.map(String)} style={{ minWidth: 160 }}
          onChange={(e) => onChange(Array.from(e.target.selectedOptions).map((o) => o.value))}>
          {field.options.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    return (
      <input placeholder="valor1, valor2" value={list.join(", ")}
        onChange={(e) => onChange(e.target.value.split(",").map((x) => cast(x.trim())).filter((x) => x !== ""))} />
    );
  }
  if (field.type === "bool")
    return (
      <select value={String(leaf.value ?? true)} onChange={(e) => onChange(e.target.value === "true")}>
        <option value="true">Sí</option>
        <option value="false">No</option>
      </select>
    );
  if (field.options?.length)
    return (
      <select value={String(leaf.value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {field.options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  return <input type={inputType} value={String(leaf.value ?? "")} onChange={(e) => onChange(cast(e.target.value))} />;
}

function LeafRow({ node, fields, onChange, onRemove }: {
  node: RuleNode; fields: SegmentField[]; onChange: (n: RuleNode) => void; onRemove: () => void;
}) {
  const negated = isNot(node);
  const leaf = (negated ? node.not : node) as RuleLeaf;
  const field = fields.find((f) => f.key === leaf.field);
  const wrap = (l: RuleLeaf, neg = negated): RuleNode => (neg ? { not: l } : l);
  const groups = Array.from(new Set(fields.map((f) => f.group)));
  return (
    <div className="inline rule-row">
      <select value={negated ? "not" : ""} onChange={(e) => onChange(wrap(leaf, e.target.value === "not"))} title="Negar">
        <option value="">Sí</option>
        <option value="not">No</option>
      </select>
      <select value={leaf.field} onChange={(e) => {
        const f = fields.find((x) => x.key === e.target.value);
        const op = f?.ops[0] ?? "eq";
        onChange(wrap({ field: e.target.value, op, value: defaultValue(f, op) }));
      }}>
        {groups.map((g) => (
          <optgroup key={g} label={g}>
            {fields.filter((f) => f.group === g).map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
          </optgroup>
        ))}
      </select>
      <select value={leaf.op} onChange={(e) => onChange(wrap({ ...leaf, op: e.target.value, value: defaultValue(field, e.target.value) }))}>
        {(field?.ops ?? []).map((o) => <option key={o} value={o}>{OP_LABELS[o] ?? o}</option>)}
      </select>
      <ValueInput field={field} leaf={leaf} onChange={(v) => onChange(wrap({ ...leaf, value: v }))} />
      <button className="icon" onClick={onRemove} aria-label="Quitar condición">✕</button>
    </div>
  );
}

export default function RuleBuilder({ value, fields, onChange, depth = 0, onRemove }: {
  value: Group; fields: SegmentField[]; onChange: (g: Group) => void; depth?: number; onRemove?: () => void;
}) {
  const list = items(value);
  const set = (i: number, n: RuleNode) => onChange(withItems(value, list.map((x, j) => (j === i ? n : x))));
  const remove = (i: number) => onChange(withItems(value, list.filter((_, j) => j !== i)));
  const addLeaf = () => {
    const f = fields[0];
    if (!f) return;
    onChange(withItems(value, [...list, { field: f.key, op: f.ops[0], value: defaultValue(f, f.ops[0]) }]));
  };
  return (
    <div className="rule-group" style={{ borderLeft: depth ? "3px solid var(--accent-soft)" : undefined, paddingLeft: depth ? 10 : 0 }}>
      <div className="inline">
        <span className="muted small">Cumple</span>
        <select value={"all" in value ? "all" : "any"} onChange={(e) =>
          onChange(e.target.value === "all" ? { all: list } : { any: list })}>
          <option value="all">todas las condiciones</option>
          <option value="any">alguna condición</option>
        </select>
        {onRemove && <button className="icon" onClick={onRemove} aria-label="Quitar grupo">✕</button>}
      </div>
      <div className="stack" style={{ gap: 6, marginTop: 6 }}>
        {list.map((n, i) =>
          isGroup(n) ? (
            <RuleBuilder key={i} value={n} fields={fields} depth={depth + 1} onChange={(g) => set(i, g)} onRemove={() => remove(i)} />
          ) : (
            <LeafRow key={i} node={n} fields={fields} onChange={(x) => set(i, x)} onRemove={() => remove(i)} />
          ),
        )}
      </div>
      <div className="inline" style={{ marginTop: 6 }}>
        <button onClick={addLeaf}>+ Condición</button>
        {depth < 4 && <button onClick={() => onChange(withItems(value, [...list, { any: [] }]))}>+ Grupo</button>}
      </div>
    </div>
  );
}
