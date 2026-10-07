// Conversión pura (sin importar Blockly) entre la definición JSON del flujo y el estado serializado
// de un workspace de Blockly (Blockly.serialization.workspaces.save/load). Conserva los ids de bloque.
import type {
  CatalogBlock,
  CatalogInput,
  Condition,
  FlowBlock,
  FlowCatalog,
  FlowDefinition,
  FlowScript,
  InputValue,
  Operand,
} from "@/lib/flow-types";

export const BLOCKLY_PREFIX = "wa_";
export const blocklyType = (type: string) => BLOCKLY_PREFIX + type;
export const flowType = (blocklyTypeName: string) =>
  blocklyTypeName.startsWith(BLOCKLY_PREFIX) ? blocklyTypeName.slice(BLOCKLY_PREFIX.length) : blocklyTypeName;

/** Bloque serializado de Blockly (subconjunto que usamos). */
export type BState = {
  type: string;
  id?: string;
  x?: number;
  y?: number;
  fields?: Record<string, unknown>;
  inputs?: Record<string, { block?: BState }>;
  next?: { block?: BState };
  extraState?: { options?: string[] };
  enabled?: boolean;
  disabledReasons?: string[];
};
export type WorkspaceState = { blocks?: { languageVersion: number; blocks: BState[] } };

/** Nombres de entradas de rama en Blockly. */
export const branchInputName = (branch: string) => `BR_${branch}`;
export const optionInputName = (index: number) => `OPT_${index}`;
export const OTHER_INPUT = "OPT_OTHER";
export const OTHER_BRANCH = "other";

export const LIST_TYPES = new Set(["buttons", "options"]);
export const NUMERIC_TYPES = new Set(["number", "duration"]);
export const ID_TYPES = new Set(["group", "agent", "resource", "ai_agent"]);

let counter = 0;
export function newId(prefix = "b"): string {
  counter += 1;
  return `${prefix}${Date.now().toString(36)}${counter.toString(36)}${Math.random().toString(36).slice(2, 5)}`;
}

export function catalogIndex(catalog: FlowCatalog): Map<string, CatalogBlock> {
  return new Map(catalog.blocks.map((b) => [b.type, b]));
}

// ---------------------------------------------------------------------------
// Valores de entrada ⇄ campos de Blockly
// ---------------------------------------------------------------------------
export function toFieldValue(input: CatalogInput, value: InputValue | undefined): string | number {
  if (value === null || value === undefined) return NUMERIC_TYPES.has(input.type) ? 0 : "";
  if (LIST_TYPES.has(input.type)) return Array.isArray(value) ? value.join(", ") : String(value);
  if (NUMERIC_TYPES.has(input.type)) return typeof value === "number" ? value : Number(value) || 0;
  if (typeof value === "object") return JSON.stringify(value);
  return typeof value === "boolean" ? String(value) : (value as string | number);
}

export function fromFieldValue(input: CatalogInput, raw: unknown): InputValue {
  if (raw === undefined || raw === null) return null;
  if (LIST_TYPES.has(input.type)) {
    return String(raw)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  if (NUMERIC_TYPES.has(input.type)) {
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }
  if (ID_TYPES.has(input.type)) {
    const s = String(raw).trim();
    if (!s) return null;
    return /^\d+$/.test(s) ? Number(s) : s;
  }
  const s = String(raw);
  return s === "" ? null : s;
}

function operandToField(o: Operand | undefined): string {
  if (o === null || o === undefined) return "";
  if (typeof o === "object") return JSON.stringify(o);
  return String(o);
}
function fieldToOperand(raw: unknown): Operand {
  const s = raw === undefined || raw === null ? "" : String(raw);
  if (s.startsWith('{"op"')) {
    try {
      return JSON.parse(s) as Condition;
    } catch {
      /* texto literal */
    }
  }
  return s === "" ? null : s;
}

// ---------------------------------------------------------------------------
// Condiciones (reporteros) ⇄ bloques de Blockly
// ---------------------------------------------------------------------------
function conditionToState(c: Condition | Operand | null | undefined): BState | undefined {
  if (!c || typeof c !== "object") return undefined;
  const type = blocklyType(c.op);
  if (c.op === "and" || c.op === "or") {
    const inputs: BState["inputs"] = {};
    const l = conditionToState(c.left);
    const r = conditionToState(c.right);
    if (l) inputs.left = { block: l };
    if (r) inputs.right = { block: r };
    return { type, inputs };
  }
  if (c.op === "not") {
    const v = conditionToState(c.value as Condition);
    return { type, inputs: v ? { value: { block: v } } : {} };
  }
  if (c.op === "length") return { type, fields: { value: operandToField(c.value as Operand) } };
  const cmp = c as { left: Operand; right: Operand };
  return { type, fields: { left: operandToField(cmp.left), right: operandToField(cmp.right) } };
}

function stateToCondition(s: BState | undefined): Condition | null {
  if (!s) return null;
  const op = flowType(s.type);
  const f = s.fields || {};
  const inp = s.inputs || {};
  if (op === "and" || op === "or") {
    return { op, left: stateToCondition(inp.left?.block), right: stateToCondition(inp.right?.block) };
  }
  if (op === "not") return { op, value: stateToCondition(inp.value?.block) };
  if (op === "length") return { op, value: fieldToOperand(f.value) };
  if (op === "eq" || op === "gt" || op === "lt" || op === "contains" || op === "join") {
    return { op, left: fieldToOperand(f.left), right: fieldToOperand(f.right) };
  }
  return null;
}

// ---------------------------------------------------------------------------
// Definición → Blockly
// ---------------------------------------------------------------------------
function chain(blocks: FlowBlock[], idx: Map<string, CatalogBlock>): BState | undefined {
  let head: BState | undefined;
  let tail: BState | undefined;
  for (const b of blocks) {
    const s = blockToState(b, idx);
    if (!head) head = s;
    else tail!.next = { block: s };
    tail = s;
  }
  return head;
}

function blockToState(b: FlowBlock, idx: Map<string, CatalogBlock>): BState {
  const cat = idx.get(b.type);
  const state: BState = { type: blocklyType(b.type), id: b.id, fields: {}, inputs: {} };
  if (b.disabled) state.disabledReasons = ["MANUALLY_DISABLED"];
  for (const input of cat?.inputs ?? []) {
    if (input.type === "condition") {
      const c = conditionToState(b.inputs?.[input.name] as Condition);
      if (c) state.inputs![input.name] = { block: c };
    } else {
      state.fields![input.name] = toFieldValue(input, b.inputs?.[input.name]);
    }
  }
  if (cat?.branches?.includes("options")) {
    const options = (Array.isArray(b.inputs?.options) ? (b.inputs.options as string[]) : []).filter(Boolean);
    state.extraState = { options };
    options.forEach((opt, i) => {
      const head = chain(b.branches?.[opt] ?? [], idx);
      if (head) state.inputs![optionInputName(i)] = { block: head };
    });
    const other = chain(b.branches?.[OTHER_BRANCH] ?? [], idx);
    if (other) state.inputs![OTHER_INPUT] = { block: other };
  } else {
    for (const br of cat?.branches ?? []) {
      const head = chain(b.branches?.[br] ?? [], idx);
      if (head) state.inputs![branchInputName(br)] = { block: head };
    }
  }
  return state;
}

export function definitionToBlockly(def: FlowDefinition, catalog: FlowCatalog): WorkspaceState {
  const idx = catalogIndex(catalog);
  const top: BState[] = def.scripts.map((s, n) => {
    const hatCat = idx.get(s.trigger.type);
    const hat: BState = {
      type: blocklyType(s.trigger.type),
      id: s.id,
      x: s.position?.x ?? 40,
      y: s.position?.y ?? 40 + n * 220,
      fields: {},
    };
    for (const input of hatCat?.inputs ?? []) hat.fields![input.name] = toFieldValue(input, s.trigger.config?.[input.name]);
    const head = chain(s.blocks, idx);
    if (head) hat.next = { block: head };
    return hat;
  });
  return { blocks: { languageVersion: 0, blocks: top } };
}

// ---------------------------------------------------------------------------
// Blockly → Definición
// ---------------------------------------------------------------------------
function unchain(head: BState | undefined, idx: Map<string, CatalogBlock>): FlowBlock[] {
  const out: FlowBlock[] = [];
  let cur = head;
  while (cur) {
    out.push(stateToBlock(cur, idx));
    cur = cur.next?.block;
  }
  return out;
}

function stateToBlock(s: BState, idx: Map<string, CatalogBlock>): FlowBlock {
  const type = flowType(s.type);
  const cat = idx.get(type);
  const block: FlowBlock = { id: s.id || newId(), type, inputs: {} };
  if (s.disabledReasons?.length || s.enabled === false) block.disabled = true;
  for (const input of cat?.inputs ?? []) {
    block.inputs[input.name] =
      input.type === "condition" ? stateToCondition(s.inputs?.[input.name]?.block) : fromFieldValue(input, s.fields?.[input.name]);
  }
  if (cat?.branches?.includes("options")) {
    const options = (block.inputs.options as string[] | null) ?? s.extraState?.options ?? [];
    block.branches = {};
    options.forEach((opt, i) => {
      block.branches![opt] = unchain(s.inputs?.[optionInputName(i)]?.block, idx);
    });
    block.branches[OTHER_BRANCH] = unchain(s.inputs?.[OTHER_INPUT]?.block, idx);
  } else if (cat?.branches?.length) {
    block.branches = {};
    for (const br of cat.branches) block.branches[br] = unchain(s.inputs?.[branchInputName(br)]?.block, idx);
  }
  return block;
}

export type BlocklyToDefinitionResult = { definition: FlowDefinition; orphans: number };

/** Los bloques sueltos (sin sombrero) no se guardan: se cuentan para avisar al usuario. */
export function blocklyToDefinition(
  state: WorkspaceState,
  catalog: FlowCatalog,
  variables: FlowDefinition["variables"],
): BlocklyToDefinitionResult {
  const idx = catalogIndex(catalog);
  const scripts: FlowScript[] = [];
  let orphans = 0;
  for (const top of state.blocks?.blocks ?? []) {
    const type = flowType(top.type);
    const cat = idx.get(type);
    if (cat?.shape !== "hat") {
      orphans += 1;
      continue;
    }
    const config: Record<string, InputValue> = {};
    for (const input of cat.inputs) config[input.name] = fromFieldValue(input, top.fields?.[input.name]);
    scripts.push({
      id: top.id || newId("s"),
      trigger: { type, config },
      blocks: unchain(top.next?.block, idx),
      position: { x: Math.round(top.x ?? 0), y: Math.round(top.y ?? 0) },
    });
  }
  return { definition: { schema_version: 1, variables, scripts }, orphans };
}

// ---------------------------------------------------------------------------
// Utilidades sobre la definición
// ---------------------------------------------------------------------------
export function emptyDefinition(triggerType = "inbound_message"): FlowDefinition {
  return { schema_version: 1, variables: [], scripts: [{ id: newId("s"), trigger: { type: triggerType, config: {} }, blocks: [] }] };
}

/** Recorre todos los bloques (incluidas ramas). */
export function walkBlocks(blocks: FlowBlock[], fn: (b: FlowBlock, path: string) => void, base = ""): void {
  blocks.forEach((b, i) => {
    const path = `${base}[${i}]`;
    fn(b, path);
    for (const [name, branch] of Object.entries(b.branches ?? {})) walkBlocks(branch, fn, `${path}.branches.${name}`);
  });
}

/** ¿Todos los bloques son aptos para el modo Junior? */
export function isJuniorCompatible(def: FlowDefinition, catalog: FlowCatalog): boolean {
  const idx = catalogIndex(catalog);
  let ok = true;
  for (const s of def.scripts) {
    if (!idx.get(s.trigger.type)?.junior) ok = false;
    walkBlocks(s.blocks, (b) => {
      if (!idx.get(b.type)?.junior) ok = false;
    });
  }
  return ok;
}

export function defaultInputs(cat: CatalogBlock): Record<string, InputValue> {
  const out: Record<string, InputValue> = {};
  for (const input of cat.inputs) {
    if (input.type === "select") out[input.name] = input.options?.[0]?.value ?? null;
    else if (LIST_TYPES.has(input.type)) out[input.name] = input.name === "options" ? ["Sí", "No"] : [];
    else if (NUMERIC_TYPES.has(input.type)) out[input.name] = input.type === "duration" ? 60 : 1;
    else out[input.name] = null;
  }
  return out;
}

export function newBlock(cat: CatalogBlock): FlowBlock {
  const b: FlowBlock = { id: newId(), type: cat.type, inputs: defaultInputs(cat) };
  if (cat.branches?.includes("options")) {
    b.branches = Object.fromEntries([...((b.inputs.options as string[]) ?? []), OTHER_BRANCH].map((o) => [o, []]));
  } else if (cat.branches?.length) {
    b.branches = Object.fromEntries(cat.branches.map((br) => [br, []]));
  }
  return b;
}
