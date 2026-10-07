"use client";

// Modo Avanzado (estilo Scratch 3) con Blockly: bloques definidos dinámicamente desde el catálogo.
import { useEffect, useRef, useState } from "react";
import type * as BlocklyT from "blockly";
import type { CatalogBlock, FlowCatalog, FlowDefinition } from "@/lib/flow-types";
import {
  blocklyToDefinition,
  blocklyType,
  branchInputName,
  definitionToBlockly,
  NUMERIC_TYPES,
  OTHER_INPUT,
  optionInputName,
  type WorkspaceState,
} from "./convert";
import styles from "./flows.module.css";

type BlocklyModule = typeof BlocklyT;
type OptionsBlock = BlocklyT.Block & { options_: string[]; updateShape_: () => void };

const BRANCH_LABEL: Record<string, string> = { then: "entonces", else: "si no", body: "repetir" };
const BOOLEAN_OPS = new Set(["eq", "gt", "lt", "contains", "and", "or", "not"]);
const THEME_NAME = "waFlows";

function parseList(raw: unknown): string[] {
  return String(raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function registerBlocks(Blockly: BlocklyModule, catalog: FlowCatalog) {
  const colorOf = (cat: string) => catalog.categories.find((c) => c.key === cat)?.color ?? "#888888";

  const defineBlock = (b: CatalogBlock) => {
    const isOptions = !!b.branches?.includes("options");
    const def: Record<string, unknown> = {
      init(this: BlocklyT.Block) {
        this.setColour(colorOf(b.category));
        this.setTooltip(b.label);
        const head = this.appendDummyInput("HEAD");
        head.appendField(b.shape === "reporter" ? b.label : `${b.icon} ${b.label}`);
        for (const input of b.inputs) {
          if (input.type === "condition") {
            this.appendValueInput(input.name).setCheck("Boolean").appendField(b.shape === "reporter" ? "" : input.label);
            continue;
          }
          const row = b.shape === "reporter" ? head : this.appendDummyInput(`IN_${input.name}`).appendField(input.label);
          if (input.type === "select" && input.options?.length) {
            row.appendField(new Blockly.FieldDropdown(input.options.map((o) => [o.label, o.value] as [string, string])), input.name);
          } else if (NUMERIC_TYPES.has(input.type)) {
            row.appendField(new Blockly.FieldNumber(0, 0), input.name);
          } else {
            row.appendField(new Blockly.FieldTextInput(""), input.name);
          }
        }
        if (b.shape === "reporter") {
          this.setOutput(true, BOOLEAN_OPS.has(b.type) ? "Boolean" : null);
          this.setInputsInline(true);
          return;
        }
        if (b.shape === "hat") {
          this.setNextStatement(true);
          return;
        }
        this.setPreviousStatement(true);
        if (b.shape !== "cap") this.setNextStatement(true);
        if (isOptions) {
          (this as OptionsBlock).options_ = [];
          (this as OptionsBlock).updateShape_();
        } else {
          for (const br of b.branches ?? []) this.appendStatementInput(branchInputName(br)).appendField(BRANCH_LABEL[br] ?? br);
        }
      },
    };

    if (isOptions) {
      def.saveExtraState = function (this: OptionsBlock) {
        return { options: this.options_ };
      };
      def.loadExtraState = function (this: OptionsBlock, state: { options?: string[] }) {
        this.options_ = state?.options ?? [];
        this.updateShape_();
      };
      def.updateShape_ = function (this: OptionsBlock) {
        // Conserva los bloques conectados (renombrando ramas por posición)
        const attached: (BlocklyT.Block | null)[] = [];
        let other: BlocklyT.Block | null = null;
        for (let i = 0; this.getInput(optionInputName(i)); i++) {
          const child = this.getInput(optionInputName(i))!.connection?.targetBlock() ?? null;
          if (child) child.unplug();
          attached.push(child);
          this.removeInput(optionInputName(i));
        }
        if (this.getInput(OTHER_INPUT)) {
          other = this.getInput(OTHER_INPUT)!.connection?.targetBlock() ?? null;
          if (other) other.unplug();
          this.removeInput(OTHER_INPUT);
        }
        this.options_.forEach((opt, i) => {
          const input = this.appendStatementInput(optionInputName(i)).appendField(`si «${opt}»`);
          const child = attached[i];
          if (child?.previousConnection) input.connection?.connect(child.previousConnection);
        });
        const otherInput = this.appendStatementInput(OTHER_INPUT).appendField("otra / sin respuesta");
        if (other?.previousConnection) otherInput.connection?.connect(other.previousConnection);
      };
      def.onchange = function (this: OptionsBlock, e: BlocklyT.Events.Abstract) {
        if (e.isUiEvent || this.isInFlyout) return;
        const next = parseList(this.getFieldValue("options"));
        if (JSON.stringify(next) !== JSON.stringify(this.options_)) {
          this.options_ = next;
          this.updateShape_();
        }
      };
    }
    Blockly.Blocks[blocklyType(b.type)] = def;
  };

  catalog.blocks.forEach(defineBlock);
}

function toolbox(catalog: FlowCatalog) {
  return {
    kind: "categoryToolbox",
    contents: catalog.categories.map((c) => ({
      kind: "category",
      name: c.label,
      colour: c.color,
      contents: catalog.blocks.filter((b) => b.category === c.key).map((b) => ({ kind: "block", type: blocklyType(b.type) })),
    })),
  };
}

export default function AdvancedEditor({
  definition,
  catalog,
  onChange,
  errors,
  readOnly,
}: {
  definition: FlowDefinition;
  catalog: FlowCatalog;
  onChange: (d: FlowDefinition, orphans: number) => void;
  errors: Map<string, string[]>;
  readOnly?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const ws = useRef<BlocklyT.WorkspaceSvg | null>(null);
  const blockly = useRef<BlocklyModule | null>(null);
  const lastEmitted = useRef<string>("");
  const loading = useRef(false);
  const latest = useRef({ definition, onChange, catalog });
  latest.current = { definition, onChange, catalog };
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Inyección (una vez por catálogo / modo lectura)
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let onResize: (() => void) | undefined;
    (async () => {
      try {
        const Blockly = await import("blockly");
        if (disposed || !host.current) return;
        blockly.current = Blockly;
        registerBlocks(Blockly, catalog);
        let theme: BlocklyT.Theme;
        try {
          theme = Blockly.Theme.defineTheme(THEME_NAME, { name: THEME_NAME, base: Blockly.Themes.Classic, startHats: true });
        } catch {
          theme = Blockly.Themes.Classic;
        }
        const workspace = Blockly.inject(host.current, {
          toolbox: readOnly ? undefined : toolbox(catalog),
          renderer: "zelos",
          theme,
          readOnly: !!readOnly,
          trashcan: !readOnly,
          zoom: { controls: true, wheel: true, startScale: 0.8 },
          move: { scrollbars: true, drag: true, wheel: false },
          grid: { spacing: 24, length: 2, colour: "#e5e7eb", snap: true },
        });
        ws.current = workspace;
        load(latest.current.definition);
        workspace.addChangeListener((e: BlocklyT.Events.Abstract) => {
          if (e.isUiEvent || loading.current) return;
          clearTimeout(timer);
          timer = setTimeout(() => {
            if (!ws.current || !blockly.current) return;
            const state = blockly.current.serialization.workspaces.save(ws.current) as WorkspaceState;
            const { definition: next, orphans } = blocklyToDefinition(state, latest.current.catalog, latest.current.definition.variables);
            lastEmitted.current = JSON.stringify(next);
            latest.current.onChange(next, orphans);
          }, 300);
        });
        onResize = () => ws.current && Blockly.svgResize(ws.current);
        window.addEventListener("resize", onResize);
        setReady(true);
      } catch (e) {
        setLoadError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      disposed = true;
      clearTimeout(timer);
      if (onResize) window.removeEventListener("resize", onResize);
      ws.current?.dispose();
      ws.current = null;
      setReady(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, readOnly]);

  function load(def: FlowDefinition) {
    if (!ws.current || !blockly.current) return;
    loading.current = true;
    try {
      blockly.current.serialization.workspaces.load(definitionToBlockly(def, latest.current.catalog), ws.current);
      lastEmitted.current = JSON.stringify(def);
    } finally {
      loading.current = false;
    }
  }

  // Cambios externos (IA, versión anterior, importación): recargar el workspace
  useEffect(() => {
    if (!ready) return;
    const incoming = JSON.stringify(definition);
    if (incoming !== lastEmitted.current) {
      const sameExceptVars =
        JSON.stringify({ ...definition, variables: [] }) === JSON.stringify({ ...JSON.parse(lastEmitted.current || "{}"), variables: [] });
      if (!sameExceptVars) load(definition);
      else lastEmitted.current = incoming;
    }
  }, [definition, ready]);

  // Errores de validación como advertencias en los bloques
  useEffect(() => {
    const workspace = ws.current;
    if (!ready || !workspace) return;
    for (const block of workspace.getAllBlocks(false)) block.setWarningText(errors.get(block.id)?.join("\n") ?? null);
  }, [errors, ready, definition]);

  return (
    <div className={styles.blockly}>
      {loadError && <div className="error-box">No se pudo cargar el editor de bloques: {loadError}</div>}
      {!ready && !loadError && <div className="empty-state muted">Cargando bloques…</div>}
      <div ref={host} className={styles.blocklyHost} aria-label="Editor de bloques avanzado" />
    </div>
  );
}
