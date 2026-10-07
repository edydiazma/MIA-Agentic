// Operaciones puras sobre el grafo del journey (el editor nunca muta la definición en su lugar).
import type { Definition, Step, StepType } from "@/lib/journey-types";

const PREFIX: Record<StepType, string> = {
  send_template: "tpl", send_text: "msg", send_email: "mail", wait: "wait", wait_until: "hour", branch: "if",
  split: "ab", update_contact: "set", add_tag: "tag", create_deal: "deal", move_stage: "stage",
  notify_agent: "notify", start_flow: "flow", exit: "exit",
};

export const isFork = (s: Step) => s.type === "branch" || s.type === "split";

export function newId(def: Definition, type: StepType): string {
  const used = new Set(def.steps.map((s) => s.id));
  let n = 1;
  while (used.has(`${PREFIX[type]}${n}`)) n++;
  return `${PREFIX[type]}${n}`;
}

export function defaultStep(type: StepType, id: string): Step {
  switch (type) {
    case "send_template":
      return { id, type, config: { template_name: "", language: "es", params: [] } };
    case "send_text":
      return { id, type, config: { text: "Hola {{nombre}}", channel: "auto" } };
    case "send_email":
      return { id, type, config: { subject: "", body: "" } };
    case "wait":
      return { id, type, config: { days: 1 } };
    case "wait_until":
      return { id, type, config: { time: "10:00" } };
    case "branch":
      return { id, type, config: { condition: { type: "replied", within_hours: 24 } }, branches: { yes: null, no: null } };
    case "split":
      return {
        id, type, config: { variants: [{ key: "A", weight: 50 }, { key: "B", weight: 50 }] }, branches: { A: null, B: null },
      };
    case "update_contact":
      return { id, type, config: { field: "stage", value: "prospect" } };
    case "add_tag":
      return { id, type, config: { tags: [] } };
    case "create_deal":
      return { id, type, config: { pipeline: "default", name: "Oportunidad {{nombre}}" } };
    case "move_stage":
      return { id, type, config: { pipeline: "default", stage: "" } };
    case "notify_agent":
      return { id, type, config: { to: "owner", title: "Revisa a {{nombre}}" } };
    case "start_flow":
      return { id, type, config: { flow_id: null } };
    default:
      return { id, type, config: {} };
  }
}

const clone = (def: Definition): Definition => ({ start: def.start, steps: def.steps.map((s) => ({ ...s, branches: s.branches && { ...s.branches } })) });

/** Inserta un paso nuevo después de `parent` (o en su rama `branch`; o al inicio si parent es null). */
export function insertStep(def: Definition, parent: string | null, branch: string | null, type: StepType): [Definition, string] {
  const d = clone(def);
  const id = newId(d, type);
  const step = defaultStep(type, id);
  let continuation: string | null = null;
  if (parent === null) {
    continuation = d.start || null;
    d.start = id;
  } else {
    const p = d.steps.find((s) => s.id === parent)!;
    if (branch !== null) {
      continuation = p.branches?.[branch] ?? null;
      p.branches = { ...(p.branches ?? {}), [branch]: id };
    } else {
      continuation = p.next ?? null;
      p.next = id;
    }
  }
  if (isFork(step)) {
    const first = Object.keys(step.branches!)[0];
    step.branches = { ...step.branches, [first]: continuation };
  } else if (type !== "exit") {
    step.next = continuation;
  }
  d.steps.push(step);
  return [prune(d), id];
}

export function reachable(def: Definition): Set<string> {
  const by = new Map(def.steps.map((s) => [s.id, s]));
  const seen = new Set<string>();
  const stack = [def.start];
  while (stack.length) {
    const id = stack.pop();
    if (!id || seen.has(id) || !by.has(id)) continue;
    seen.add(id);
    const s = by.get(id)!;
    stack.push(...[s.next, ...Object.values(s.branches ?? {})].filter((x): x is string => !!x));
  }
  return seen;
}

function prune(d: Definition): Definition {
  const keep = reachable(d);
  return { ...d, steps: d.steps.filter((s) => keep.has(s.id)) };
}

/** Quita un paso; lo que venía después (o su primera rama) toma su lugar. Las otras ramas se descartan. */
export function removeStep(def: Definition, id: string): Definition {
  const d = clone(def);
  const s = d.steps.find((x) => x.id === id);
  if (!s) return def;
  const cont = isFork(s) ? (Object.values(s.branches ?? {})[0] ?? null) : (s.next ?? null);
  const swap = (v: string | null | undefined) => (v === id ? cont : v);
  if (d.start === id) d.start = cont ?? "";
  for (const x of d.steps) {
    if (x.next !== undefined) x.next = swap(x.next);
    if (x.branches) x.branches = Object.fromEntries(Object.entries(x.branches).map(([k, v]) => [k, swap(v) ?? null]));
  }
  d.steps = d.steps.filter((x) => x.id !== id);
  return prune(d);
}

export function updateStep(def: Definition, id: string, patch: Partial<Step>): Definition {
  return { ...def, steps: def.steps.map((s) => (s.id === id ? { ...s, ...patch } : s)) };
}

/** Errores del servidor agrupados por paso ("s1: falta …") y generales. */
export function errorsByStep(errors: string[]): { byStep: Record<string, string[]>; general: string[] } {
  const byStep: Record<string, string[]> = {};
  const general: string[] = [];
  for (const e of errors) {
    const m = /^([A-Za-z0-9_-]{1,40}): (.*)$/.exec(e);
    if (m) (byStep[m[1]] ??= []).push(m[2]);
    else general.push(e);
  }
  return { byStep, general };
}
