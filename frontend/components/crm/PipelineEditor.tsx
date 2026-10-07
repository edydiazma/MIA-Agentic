"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import type { PipelineStage, PipelinesConfig } from "@/lib/crm-types";
import { ErrorBox, Field, Modal, useAction } from "@/components/ui";

const FIXED = new Set(["won", "lost"]);

const slug = (s: string) =>
  s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 40);

/** Editor de etapas del embudo principal. «Ganado» y «Perdido» son obligatorias (cierran el negocio). */
export default function PipelineEditor({
  config,
  pipeline = "default",
  onClose,
  onSaved,
}: {
  config: PipelinesConfig;
  pipeline?: string;
  onClose: () => void;
  onSaved: (c: PipelinesConfig) => void;
}) {
  const p = config.pipelines[pipeline];
  const [label, setLabel] = useState(p?.label ?? "Ventas");
  const [currency, setCurrency] = useState(config.currency);
  const [stages, setStages] = useState<PipelineStage[]>(p?.stages ?? []);
  const [run, busy, error] = useAction();

  const open = stages.filter((s) => !FIXED.has(s.key));
  const closed = stages.filter((s) => FIXED.has(s.key));
  const setOpen = (next: PipelineStage[]) => setStages([...next, ...closed]);

  function update(i: number, labelValue: string) {
    const next = [...open];
    const isNew = !p?.stages.some((s) => s.key === next[i].key);
    next[i] = { key: isNew ? slug(labelValue) || next[i].key : next[i].key, label: labelValue };
    setOpen(next);
  }
  function moveStage(i: number, dir: -1 | 1) {
    const next = [...open];
    const j = i + dir;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    setOpen(next);
  }

  async function save() {
    const body = {
      pipelines: { ...config.pipelines, [pipeline]: { label: label.trim() || "Ventas", stages } },
      currency: currency.trim().toUpperCase() || "COP",
    };
    const c = await run(() => send<PipelinesConfig>("/api/deals/pipelines", "PUT", body));
    if (c) onSaved(c);
  }

  return (
    <Modal
      title="Etapas de negocios"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || open.some((s) => !s.label.trim() || !s.key)} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <div className="form">
        <div className="grid2">
          <Field label="Nombre del embudo">
            <input value={label} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field label="Moneda por defecto">
            <input maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
          </Field>
        </div>
        <Field label="Etapas abiertas" hint="Los negocios avanzan en este orden en el tablero.">
          <div className="stack" style={{ gap: 6 }}>
            {open.map((s, i) => (
              <div key={i} className="inline">
                <input value={s.label} onChange={(e) => update(i, e.target.value)} style={{ flex: 1 }} />
                <button className="icon" aria-label="Subir" disabled={i === 0} onClick={() => moveStage(i, -1)}>
                  ↑
                </button>
                <button className="icon" aria-label="Bajar" disabled={i === open.length - 1} onClick={() => moveStage(i, 1)}>
                  ↓
                </button>
                <button className="icon" aria-label="Quitar" disabled={open.length <= 1} onClick={() => setOpen(open.filter((_, j) => j !== i))}>
                  ✕
                </button>
              </div>
            ))}
            <button className="link small" style={{ alignSelf: "flex-start" }} onClick={() => setOpen([...open, { key: "", label: "" }])}>
              + Agregar etapa
            </button>
          </div>
        </Field>
        <Field label="Etapas de cierre" hint="Fijas: al mover un negocio aquí se marca como ganado o perdido.">
          <div className="stack" style={{ gap: 6 }}>
            {closed.map((s) => (
              <input
                key={s.key}
                value={s.label}
                onChange={(e) => setStages(stages.map((x) => (x.key === s.key ? { ...x, label: e.target.value } : x)))}
              />
            ))}
          </div>
        </Field>
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
