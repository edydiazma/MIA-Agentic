// Catálogo de bloques (respaldo local de GET /api/flows/catalog). Ver docs/flows.md §3.
// Fuente única: lib/flow-blocks.json es copia exacta de backend/app/flows/blocks.json
// (tests/test_flows.py::test_catalog_parity falla si difieren).
import data from "./flow-blocks.json";
import type { FlowCatalog } from "./flow-types";

export const FALLBACK_CATALOG = data as unknown as FlowCatalog;
const blocks = FALLBACK_CATALOG.blocks;

export const TRIGGER_TYPES: { value: string; label: string }[] = blocks
  .filter((b) => b.shape === "hat")
  .map((b) => ({ value: b.type, label: b.label }));

export const CONDITION_OPS = ["eq", "gt", "lt", "contains", "and", "or", "not", "join", "length"] as const;
