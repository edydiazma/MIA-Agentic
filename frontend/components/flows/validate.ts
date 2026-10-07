// Validación en el cliente (la definitiva la hace el servidor) y mapeo de errores a bloques.
import type { FlowBlock, FlowCatalog, FlowDefinition, FlowError } from "@/lib/flow-types";
import { catalogIndex, LIST_TYPES, walkBlocks } from "./convert";

function isEmpty(v: unknown): boolean {
  if (v === null || v === undefined) return true;
  if (typeof v === "string") return v.trim() === "";
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

export function validateDefinition(def: FlowDefinition, catalog: FlowCatalog): FlowError[] {
  const idx = catalogIndex(catalog);
  const errors: FlowError[] = [];
  const seen = new Map<string, string>();

  const checkId = (id: string, path: string) => {
    if (!id) errors.push({ path, message: "Falta el id del bloque" });
    else if (seen.has(id)) errors.push({ path, block_id: id, message: `Id repetido (también en ${seen.get(id)})` });
    else seen.set(id, path);
  };

  if (!def.scripts.length) errors.push({ path: "scripts", message: "El flujo necesita al menos un evento de inicio" });

  def.scripts.forEach((s, si) => {
    const sp = `scripts[${si}]`;
    checkId(s.id, sp);
    const hat = idx.get(s.trigger.type);
    if (!hat || hat.shape !== "hat") {
      errors.push({ path: `${sp}.trigger`, block_id: s.id, message: `Evento de inicio desconocido: ${s.trigger.type}` });
    } else {
      for (const input of hat.inputs) {
        if (input.required && isEmpty(s.trigger.config?.[input.name])) {
          errors.push({ path: `${sp}.trigger.config.${input.name}`, block_id: s.id, message: `Falta «${input.label}»` });
        }
      }
    }
    walkBlocks(s.blocks, (b: FlowBlock, path) => {
      const bp = `${sp}.blocks${path}`;
      checkId(b.id, bp);
      const cat = idx.get(b.type);
      if (!cat) {
        errors.push({ path: bp, block_id: b.id, message: `Bloque desconocido: ${b.type}` });
        return;
      }
      if (cat.shape === "hat") errors.push({ path: bp, block_id: b.id, message: "Solo puede haber un evento de inicio por guion" });
      if (cat.shape === "reporter") errors.push({ path: bp, block_id: b.id, message: "Los operadores solo van dentro de una condición" });
      for (const input of cat.inputs) {
        if (input.required && isEmpty(b.inputs?.[input.name])) {
          errors.push({ path: `${bp}.inputs.${input.name}`, block_id: b.id, message: `${cat.label}: falta «${input.label}»` });
        }
        if (b.type === "send_buttons" && input.name === "buttons" && LIST_TYPES.has(input.type)) {
          const n = (b.inputs?.buttons as string[] | undefined)?.length ?? 0;
          if (n > 3) errors.push({ path: `${bp}.inputs.buttons`, block_id: b.id, message: "WhatsApp permite máximo 3 botones" });
        }
      }
      if (b.type === "repeat" && Number(b.inputs?.times) > 20) {
        errors.push({ path: `${bp}.inputs.times`, block_id: b.id, message: "Máximo 20 repeticiones" });
      }
    });
  });
  return errors;
}

/** Convierte una ruta del servidor (p. ej. "scripts[0].blocks[2].branches.then[0].inputs.text") en el id del bloque. */
export function blockIdForPath(def: FlowDefinition, path: string): string | undefined {
  const tokens = path.match(/scripts\[(\d+)\](.*)/);
  if (!tokens) return undefined;
  const script = def.scripts[Number(tokens[1])];
  if (!script) return undefined;
  let rest = tokens[2];
  let list: FlowBlock[] = script.blocks;
  let found: string | undefined = script.id;
  const re = /^(?:\.blocks)?\[(\d+)\]|^\.branches\.([^.[]+)/;
  while (rest) {
    const m = rest.match(re);
    if (!m) break;
    if (m[1] !== undefined) {
      const b = list[Number(m[1])];
      if (!b) break;
      found = b.id;
      list = [];
      rest = rest.slice(m[0].length);
      const branch = rest.match(/^\.branches\.([^.[]+)/);
      if (branch) {
        list = b.branches?.[branch[1]] ?? [];
        rest = rest.slice(branch[0].length);
      } else break;
    } else break;
  }
  return found;
}

export function errorsByBlock(def: FlowDefinition, errors: FlowError[]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const e of errors) {
    const id = e.block_id ?? blockIdForPath(def, e.path);
    if (!id) continue;
    map.set(id, [...(map.get(id) ?? []), e.message]);
  }
  return map;
}
