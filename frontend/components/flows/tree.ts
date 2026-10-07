// Operaciones inmutables sobre la definición: listas de bloques identificadas por una clave.
//   "s:<scriptId>"             → bloques principales de un guion
//   "b:<blockId>:<branchName>" → una rama de un bloque (then/else/body/opción/other)
import type { FlowBlock, FlowDefinition } from "@/lib/flow-types";

export const scriptKey = (scriptId: string) => `s:${scriptId}`;
export const branchKey = (blockId: string, branch: string) => `b:${blockId}:${branch}`;

function mapBlocks(blocks: FlowBlock[], key: string, fn: (list: FlowBlock[]) => FlowBlock[]): FlowBlock[] {
  return blocks.map((b) => {
    if (!b.branches) return b;
    let changed = false;
    const branches: Record<string, FlowBlock[]> = {};
    for (const [name, list] of Object.entries(b.branches)) {
      let next = branchKey(b.id, name) === key ? fn(list) : list;
      next = mapBlocks(next, key, fn);
      if (next !== list) changed = true;
      branches[name] = next;
    }
    return changed ? { ...b, branches } : b;
  });
}

export function updateList(def: FlowDefinition, key: string, fn: (list: FlowBlock[]) => FlowBlock[]): FlowDefinition {
  return {
    ...def,
    scripts: def.scripts.map((s) => {
      const blocks = scriptKey(s.id) === key ? fn(s.blocks) : s.blocks;
      return { ...s, blocks: mapBlocks(blocks, key, fn) };
    }),
  };
}

export type Located = { key: string; index: number; block: FlowBlock };

export function findBlock(def: FlowDefinition, id: string): Located | null {
  const search = (list: FlowBlock[], key: string): Located | null => {
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      if (b.id === id) return { key, index: i, block: b };
      for (const [name, branch] of Object.entries(b.branches ?? {})) {
        const found = search(branch, branchKey(b.id, name));
        if (found) return found;
      }
    }
    return null;
  };
  for (const s of def.scripts) {
    const found = search(s.blocks, scriptKey(s.id));
    if (found) return found;
  }
  return null;
}

export function getList(def: FlowDefinition, key: string): FlowBlock[] | null {
  if (key.startsWith("s:")) return def.scripts.find((s) => scriptKey(s.id) === key)?.blocks ?? null;
  const [, blockId, ...rest] = key.split(":");
  const found = findBlock(def, blockId);
  return found?.block.branches?.[rest.join(":")] ?? null;
}

/** ¿`key` está dentro del bloque `blockId` (no se puede mover un bloque dentro de sí mismo)? */
export function isInside(def: FlowDefinition, blockId: string, key: string): boolean {
  if (!key.startsWith("b:")) return false;
  let current = key.split(":")[1];
  while (current) {
    if (current === blockId) return true;
    const loc = findBlock(def, current);
    if (!loc || !loc.key.startsWith("b:")) return false;
    current = loc.key.split(":")[1];
  }
  return false;
}

export function insertBlock(def: FlowDefinition, key: string, index: number, block: FlowBlock): FlowDefinition {
  return updateList(def, key, (list) => {
    const next = [...list];
    next.splice(Math.max(0, Math.min(index, next.length)), 0, block);
    return next;
  });
}

export function removeBlock(def: FlowDefinition, id: string): { def: FlowDefinition; block: FlowBlock | null } {
  const loc = findBlock(def, id);
  if (!loc) return { def, block: null };
  return { def: updateList(def, loc.key, (list) => list.filter((b) => b.id !== id)), block: loc.block };
}

export function moveBlock(def: FlowDefinition, id: string, key: string, index: number): FlowDefinition {
  if (isInside(def, id, key)) return def;
  const loc = findBlock(def, id);
  if (!loc) return def;
  const adjusted = loc.key === key && loc.index < index ? index - 1 : index;
  const { def: without, block } = removeBlock(def, id);
  return block ? insertBlock(without, key, adjusted, block) : def;
}

export function replaceBlock(def: FlowDefinition, id: string, fn: (b: FlowBlock) => FlowBlock): FlowDefinition {
  const loc = findBlock(def, id);
  if (!loc) return def;
  return updateList(def, loc.key, (list) => list.map((b) => (b.id === id ? fn(b) : b)));
}
