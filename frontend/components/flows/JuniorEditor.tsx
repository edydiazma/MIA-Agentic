"use client";

import { useMemo, useState } from "react";
import type { CatalogBlock, FlowBlock, FlowCatalog, FlowDefinition, InputValue } from "@/lib/flow-types";
import BlockInputsForm from "./BlockInputsForm";
import { catalogIndex, newBlock, newId, OTHER_BRANCH } from "./convert";
import { branchKey, findBlock, getList, insertBlock, moveBlock, removeBlock, replaceBlock, scriptKey, updateList } from "./tree";
import styles from "./flows.module.css";

type Selection = { kind: "block"; id: string } | { kind: "hat"; scriptId: string } | null;
type Target = { key: string; index: number };

const BRANCH_LABEL: Record<string, string> = { then: "Sí", else: "Si no", body: "Repetir", [OTHER_BRANCH]: "Otra / sin respuesta" };

/** Al editar las opciones de "Si responde…", renombra las ramas por posición y conserva sus bloques. */
export function syncOptionBranches(block: FlowBlock, oldOptions: string[], newOptions: string[]): FlowBlock {
  const branches: Record<string, FlowBlock[]> = {};
  newOptions.forEach((opt, i) => {
    if (!opt) return;
    branches[opt] = block.branches?.[opt] ?? block.branches?.[oldOptions[i]] ?? [];
  });
  branches[OTHER_BRANCH] = block.branches?.[OTHER_BRANCH] ?? [];
  return { ...block, branches };
}

export default function JuniorEditor({
  definition,
  catalog,
  onChange,
  errors,
  readOnly,
}: {
  definition: FlowDefinition;
  catalog: FlowCatalog;
  onChange: (d: FlowDefinition) => void;
  errors: Map<string, string[]>;
  readOnly?: boolean;
}) {
  const idx = useMemo(() => catalogIndex(catalog), [catalog]);
  const juniorCats = catalog.categories.filter((c) => c.key !== "operators" && catalog.blocks.some((b) => b.category === c.key && b.junior));
  const color = (category: string) => catalog.categories.find((c) => c.key === category)?.color ?? "#888";
  const [tab, setTab] = useState(juniorCats[1]?.key ?? juniorCats[0]?.key ?? "messages");
  const [selected, setSelected] = useState<Selection>(null);
  const [target, setTarget] = useState<Target | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);

  const firstKey = definition.scripts[0] ? scriptKey(definition.scripts[0].id) : null;
  const effectiveTarget: Target | null =
    target ?? (firstKey ? { key: firstKey, index: getList(definition, firstKey)?.length ?? 0 } : null);

  // ---- acciones ----
  function addFromPalette(cat: CatalogBlock) {
    if (readOnly) return;
    if (cat.shape === "hat") {
      const sid = newId("s");
      onChange({ ...definition, scripts: [...definition.scripts, { id: sid, trigger: { type: cat.type, config: {} }, blocks: [] }] });
      setSelected({ kind: "hat", scriptId: sid });
      return;
    }
    if (!effectiveTarget) return;
    const block = newBlock(cat);
    onChange(insertBlock(definition, effectiveTarget.key, effectiveTarget.index, block));
    setTarget({ key: effectiveTarget.key, index: effectiveTarget.index + 1 });
    setSelected({ kind: "block", id: block.id });
  }

  function handleDrop(key: string, index: number, data: string) {
    if (readOnly) return;
    if (data.startsWith("move:")) {
      onChange(moveBlock(definition, data.slice(5), key, index));
    } else if (data.startsWith("new:")) {
      const cat = idx.get(data.slice(4));
      if (cat && cat.shape !== "hat") {
        const block = newBlock(cat);
        onChange(insertBlock(definition, key, index, block));
        setSelected({ kind: "block", id: block.id });
      }
    }
  }

  function deleteBlock(id: string) {
    onChange(removeBlock(definition, id).def);
    if (selected?.kind === "block" && selected.id === id) setSelected(null);
  }

  function nudge(id: string, delta: number) {
    const loc = findBlock(definition, id);
    if (!loc) return;
    const list = getList(definition, loc.key) ?? [];
    const to = Math.max(0, Math.min(list.length - 1, loc.index + delta));
    if (to === loc.index) return;
    onChange(updateList(definition, loc.key, (l) => {
      const next = [...l];
      const [b] = next.splice(loc.index, 1);
      next.splice(to, 0, b);
      return next;
    }));
  }

  function setInput(id: string, name: string, value: InputValue) {
    onChange(
      replaceBlock(definition, id, (b) => {
        const next = { ...b, inputs: { ...b.inputs, [name]: value } };
        const cat = idx.get(b.type);
        if (name === "options" && cat?.branches?.includes("options")) {
          return syncOptionBranches(next, (b.inputs.options as string[]) ?? [], (value as string[]) ?? []);
        }
        return next;
      }),
    );
  }

  // ---- render ----
  const dropProps = (slotId: string, key: string, index: number) => ({
    onDragOver: (e: React.DragEvent) => {
      if (readOnly) return;
      e.preventDefault();
      setDragOver(slotId);
    },
    onDragLeave: () => setDragOver((s) => (s === slotId ? null : s)),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(null);
      handleDrop(key, index, e.dataTransfer.getData("text/plain"));
    },
  });

  function Tile({ block, cat }: { block: FlowBlock; cat: CatalogBlock | undefined }) {
    const isSel = selected?.kind === "block" && selected.id === block.id;
    const cls = [
      styles.jblock,
      cat?.shape === "cap" ? styles.cap : "",
      isSel ? styles.selected : "",
      errors.has(block.id) ? styles.invalid : "",
      block.disabled ? styles.disabled : "",
      dragging === block.id ? styles.dragging : "",
    ].join(" ");
    return (
      <div
        className={cls}
        style={{ background: color(cat?.category ?? "") }}
        draggable={!readOnly}
        onDragStart={(e) => {
          e.dataTransfer.setData("text/plain", `move:${block.id}`);
          setDragging(block.id);
        }}
        onDragEnd={() => setDragging(null)}
        onClick={() => setSelected(isSel ? null : { kind: "block", id: block.id })}
        title={errors.get(block.id)?.join("\n") ?? cat?.label}
        role="button"
        aria-pressed={isSel}
      >
        <span className={styles.icon}>{cat?.icon ?? "❔"}</span>
        <span className={styles.label}>{cat?.label ?? block.type}</span>
      </div>
    );
  }

  function Strip({ listKey, blocks, depth }: { listKey: string; blocks: FlowBlock[]; depth: number }) {
    const withBranches = blocks.filter((b) => b.branches && Object.keys(b.branches).length);
    const isTarget = effectiveTarget?.key === listKey;
    return (
      <>
        <div className={styles.strip}>
          {blocks.map((b, i) => (
            <span key={b.id} style={{ display: "contents" }}>
              <div className={`${styles.dropSlot} ${dragOver === `${listKey}#${i}` ? styles.over : ""}`} {...dropProps(`${listKey}#${i}`, listKey, i)} />
              <Tile block={b} cat={idx.get(b.type)} />
              {i < blocks.length - 1 && <span className={styles.arrow}>›</span>}
            </span>
          ))}
          <div
            className={`${styles.dropSlot} ${dragOver === `${listKey}#end` ? styles.over : ""}`}
            {...dropProps(`${listKey}#end`, listKey, blocks.length)}
          />
          {!readOnly && (
            <button
              type="button"
              className={`${styles.addHere} ${isTarget && effectiveTarget?.index === blocks.length ? styles.active : ""}`}
              onClick={() => setTarget({ key: listKey, index: blocks.length })}
              title="Agregar aquí los bloques de la paleta"
            >
              +
            </button>
          )}
        </div>
        {withBranches.map((b) => {
          const cat = idx.get(b.type);
          return (
            <div key={`lanes-${b.id}`} className={styles.lanes} style={{ borderLeftColor: color(cat?.category ?? "") }}>
              <span className="small muted">
                {cat?.icon} {cat?.label}
              </span>
              {Object.entries(b.branches!).map(([name, list]) => (
                <div key={name} className={styles.lane}>
                  <span className={styles.laneLabel} style={{ background: color(cat?.category ?? "") }}>
                    {BRANCH_LABEL[name] ?? name}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {depth < 6 ? (
                      <Strip listKey={branchKey(b.id, name)} blocks={list} depth={depth + 1} />
                    ) : (
                      <span className={styles.laneEmpty}>Demasiado anidado: usa el modo Avanzado</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          );
        })}
      </>
    );
  }

  const selBlock = selected?.kind === "block" ? findBlock(definition, selected.id)?.block : null;
  const selCat = selBlock ? idx.get(selBlock.type) : null;
  const selScript = selected?.kind === "hat" ? definition.scripts.find((s) => s.id === selected.scriptId) : null;
  const hatCat = selScript ? idx.get(selScript.trigger.type) : null;

  return (
    <div className={styles.junior}>
      <div className={styles.stage}>
        {definition.scripts.map((s, n) => {
          const hat = idx.get(s.trigger.type);
          const hatSel = selected?.kind === "hat" && selected.scriptId === s.id;
          return (
            <div key={s.id} className={styles.script}>
              <div className={styles.scriptHead}>
                Inicio {n + 1}
                {!readOnly && definition.scripts.length > 1 && (
                  <button
                    type="button"
                    className="link small"
                    onClick={() => onChange({ ...definition, scripts: definition.scripts.filter((x) => x.id !== s.id) })}
                  >
                    quitar
                  </button>
                )}
              </div>
              <div style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
                <div
                  className={`${styles.jblock} ${styles.hat} ${hatSel ? styles.selected : ""} ${errors.has(s.id) ? styles.invalid : ""}`}
                  style={{ background: color("events") }}
                  onClick={() => setSelected(hatSel ? null : { kind: "hat", scriptId: s.id })}
                  role="button"
                  title={errors.get(s.id)?.join("\n") ?? hat?.label}
                >
                  <span className={styles.icon}>{hat?.icon ?? "🚩"}</span>
                  <span className={styles.label}>{hat?.label ?? s.trigger.type}</span>
                </div>
                <span className={styles.arrow}>›</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Strip listKey={scriptKey(s.id)} blocks={s.blocks} depth={0} />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {selBlock && selCat && (
        <div className={styles.form}>
          <div className={styles.formHead}>
            <strong>
              {selCat.icon} {selCat.label}
            </strong>
            {!readOnly && (
              <div className={styles.formActions}>
                <button type="button" onClick={() => nudge(selBlock.id, -1)} aria-label="Mover a la izquierda">
                  ←
                </button>
                <button type="button" onClick={() => nudge(selBlock.id, 1)} aria-label="Mover a la derecha">
                  →
                </button>
                <button
                  type="button"
                  onClick={() => onChange(replaceBlock(definition, selBlock.id, (b) => ({ ...b, disabled: !b.disabled })))}
                >
                  {selBlock.disabled ? "Activar" : "Desactivar"}
                </button>
                <button type="button" className="danger" onClick={() => deleteBlock(selBlock.id)}>
                  Eliminar
                </button>
              </div>
            )}
          </div>
          <BlockInputsForm
            inputs={selCat.inputs}
            values={selBlock.inputs}
            readOnly={readOnly}
            errors={errors.get(selBlock.id)}
            onChange={(name, v) => setInput(selBlock.id, name, v)}
          />
        </div>
      )}

      {selScript && hatCat && (
        <div className={styles.form}>
          <div className={styles.formHead}>
            <strong>
              {hatCat.icon} Evento de inicio
            </strong>
          </div>
          <select
            disabled={readOnly}
            value={selScript.trigger.type}
            onChange={(e) =>
              onChange({
                ...definition,
                scripts: definition.scripts.map((s) => (s.id === selScript.id ? { ...s, trigger: { type: e.target.value, config: {} } } : s)),
              })
            }
          >
            {catalog.blocks
              .filter((b) => b.shape === "hat" && (b.junior || b.type === selScript.trigger.type))
              .map((b) => (
                <option key={b.type} value={b.type}>
                  {b.icon} {b.label}
                </option>
              ))}
          </select>
          <BlockInputsForm
            inputs={hatCat.inputs}
            values={selScript.trigger.config}
            readOnly={readOnly}
            errors={errors.get(selScript.id)}
            onChange={(name, v) =>
              onChange({
                ...definition,
                scripts: definition.scripts.map((s) =>
                  s.id === selScript.id ? { ...s, trigger: { ...s.trigger, config: { ...s.trigger.config, [name]: v } } } : s,
                ),
              })
            }
          />
        </div>
      )}

      {!readOnly && (
        <div className={styles.palette}>
          <div className={styles.paletteTabs} role="tablist">
            {juniorCats.map((c) => (
              <button
                key={c.key}
                type="button"
                role="tab"
                aria-selected={tab === c.key}
                className={tab === c.key ? styles.on : ""}
                style={{ background: c.color }}
                onClick={() => setTab(c.key)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <div className={styles.paletteBlocks}>
            {catalog.blocks
              .filter((b) => b.category === tab && b.junior)
              .map((b) => (
                <div
                  key={b.type}
                  className={`${styles.jblock} ${b.shape === "hat" ? styles.hat : b.shape === "cap" ? styles.cap : ""}`}
                  style={{ background: color(b.category) }}
                  draggable={b.shape !== "hat"}
                  onDragStart={(e) => e.dataTransfer.setData("text/plain", `new:${b.type}`)}
                  onClick={() => addFromPalette(b)}
                  role="button"
                  title={b.shape === "hat" ? "Agregar un nuevo inicio" : "Toca para agregar (o arrastra)"}
                >
                  <span className={styles.icon}>{b.icon}</span>
                  <span className={styles.label}>{b.label}</span>
                </div>
              ))}
          </div>
          <div
            className={`${styles.trash} ${dragOver === "trash" ? styles.over : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver("trash");
            }}
            onDragLeave={() => setDragOver(null)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(null);
              const data = e.dataTransfer.getData("text/plain");
              if (data.startsWith("move:")) deleteBlock(data.slice(5));
            }}
          >
            🗑️ Arrastra aquí para eliminar
          </div>
        </div>
      )}
    </div>
  );
}
