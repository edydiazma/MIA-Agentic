import type { DiffRow } from "@/lib/ai-types";

/** Diferencias campo a campo entre dos documentos JSON (para comparar revisiones). */
export function jsonDiff(a: unknown, b: unknown, path = ""): DiffRow[] {
  const isObj = (x: unknown) => x !== null && typeof x === "object";
  if (!isObj(a) || !isObj(b) || Array.isArray(a) !== Array.isArray(b)) {
    return JSON.stringify(a) === JSON.stringify(b) ? [] : [{ path: path || "(raíz)", before: a, after: b }];
  }
  const keys = new Set([...Object.keys(a as object), ...Object.keys(b as object)]);
  const out: DiffRow[] = [];
  for (const k of keys) {
    const p = Array.isArray(a) ? `${path}[${k}]` : path ? `${path}.${k}` : k;
    out.push(...jsonDiff((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k], p));
  }
  return out;
}
