"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { Badge, ErrorBox, Field, Loading, Modal, useAction } from "@/components/ui";
import { useMe } from "@/components/Shell";
import JsonEditWithAI from "@/components/ai/JsonEditWithAI";
import type { EditorMode, FlowDefinition, FlowDetail, FlowError } from "@/lib/flow-types";
import { FlowValidationError, flowsApi, saveVersion, useCatalog } from "./api";
import { emptyDefinition, isJuniorCompatible } from "./convert";
import JuniorEditor from "./JuniorEditor";
import { Simulator, VariablesPanel, VersionsDrawer } from "./SidePanels";
import { STATUS } from "./FlowList";
import { errorsByBlock, validateDefinition } from "./validate";
import styles from "./flows.module.css";

// Blockly solo en el navegador
const AdvancedEditor = dynamic(() => import("./AdvancedEditor"), { ssr: false, loading: () => <Loading /> });

function download(name: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function looksLikeDefinition(x: unknown): x is FlowDefinition {
  const d = x as FlowDefinition;
  return !!d && typeof d === "object" && Array.isArray(d.scripts) && d.scripts.every((s) => s && s.trigger && Array.isArray(s.blocks));
}

export default function FlowEditor({ flowId }: { flowId: number }) {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const catalog = useCatalog();
  const [flow, setFlow] = useState<FlowDetail | null>(null);
  const [definition, setDefinition] = useState<FlowDefinition | null>(null);
  const [mode, setMode] = useState<EditorMode>("junior");
  const [dirty, setDirty] = useState(false);
  const [orphans, setOrphans] = useState(0);
  const [serverErrors, setServerErrors] = useState<FlowError[]>([]);
  const [savedVersionId, setSavedVersionId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [showAI, setShowAI] = useState(false);
  const [saveModal, setSaveModal] = useState(false);
  const [note, setNote] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [run, busy, error, setError] = useAction();
  const fileInput = useRef<HTMLInputElement>(null);
  const readOnly = !isAdmin;

  const load = useCallback(async () => {
    try {
      const d = await flowsApi.get(flowId);
      setFlow(d);
      setDefinition(d.definition ?? emptyDefinition(d.trigger_type));
      setMode(d.editor_mode);
      setDirty(false);
      setServerErrors([]);
      setSavedVersionId(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [flowId]);

  useEffect(() => {
    load();
  }, [load]);

  // Aviso al salir con cambios sin guardar
  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  const clientErrors = useMemo(() => (definition ? validateDefinition(definition, catalog) : []), [definition, catalog]);
  const allErrors = useMemo(() => [...clientErrors, ...serverErrors], [clientErrors, serverErrors]);
  const blockErrors = useMemo(() => (definition ? errorsByBlock(definition, allErrors) : new Map()), [definition, allErrors]);
  const juniorOk = definition ? isJuniorCompatible(definition, catalog) : true;

  function update(next: FlowDefinition, nextOrphans?: number) {
    setDefinition(next);
    setDirty(true);
    setServerErrors([]);
    if (nextOrphans !== undefined) setOrphans(nextOrphans);
  }

  async function switchMode(m: EditorMode) {
    setMode(m);
    if (isAdmin && flow && flow.editor_mode !== m) {
      flowsApi.update(flow.id, { editor_mode: m }).then((f) => setFlow((cur) => (cur ? { ...cur, ...f } : cur))).catch(() => {});
    }
  }

  async function save(changeNote: string): Promise<number | null> {
    if (!definition || !flow) return null;
    if (clientErrors.length) {
      setError(`Corrige ${clientErrors.length} error(es) antes de guardar`);
      return null;
    }
    try {
      const r = await run(async () => {
        try {
          return await saveVersion(flow.id, definition, changeNote);
        } catch (e) {
          if (e instanceof FlowValidationError) {
            setServerErrors(e.errors);
            throw new Error("El servidor encontró errores en el flujo (marcados en los bloques)");
          }
          throw e;
        }
      });
      if (!r) return null;
      if (r.errors?.length) setServerErrors(r.errors);
      setDirty(false);
      setSavedVersionId(r.id);
      setNotice(`Versión v${r.version} guardada`);
      setFlow((f) => (f ? { ...f, versions_count: (f.versions_count ?? 0) + 1 } : f));
      return r.id;
    } finally {
      setSaveModal(false);
      setNote("");
    }
  }

  async function publish() {
    if (!flow) return;
    let versionId = savedVersionId;
    if (dirty || !versionId) {
      versionId = dirty ? await save(note || "Publicación") : null;
      if (!versionId && !dirty) {
        const versions = await run(() => flowsApi.versions(flow.id));
        versionId = versions?.reduce<number | null>((acc, v) => (acc === null || v.id > acc ? v.id : acc), null) ?? null;
      }
    }
    if (!versionId) return;
    const f = await run(() => flowsApi.publish(flow.id, versionId!));
    if (f) {
      setFlow((cur) => (cur ? { ...cur, ...f } : cur));
      setNotice("Flujo publicado: ya se ejecuta con los mensajes nuevos");
    }
  }

  async function importFile(file: File) {
    try {
      const parsed = JSON.parse(await file.text());
      const def = looksLikeDefinition(parsed) ? parsed : looksLikeDefinition(parsed?.definition) ? parsed.definition : null;
      if (!def) throw new Error("El archivo no tiene el formato de un flujo (scripts con trigger y blocks)");
      update({ schema_version: 1, variables: def.variables ?? [], scripts: def.scripts });
      setNotice("JSON importado: revisa y guarda una versión");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (loadError) return <ErrorBox error={loadError} />;
  if (!flow || !definition) return <Loading />;

  return (
    <div className={styles.editor}>
      <div className={styles.toolbar}>
        <Link href="/automatizaciones/flujos">← Flujos</Link>
        <strong>{flow.name}</strong>
        <Badge tone={STATUS[flow.status].tone}>{STATUS[flow.status].label}</Badge>
        {flow.current_version && <span className="small muted">publicada v{flow.current_version.version}</span>}
        {dirty && <Badge tone="warn">sin guardar</Badge>}
        <span className={styles.spacer} />
        <div className={styles.modeSwitch} role="group" aria-label="Modo del editor">
          <button className={mode === "junior" ? styles.on : ""} onClick={() => switchMode("junior")}>
            🧸 Junior
          </button>
          <button className={mode === "advanced" ? styles.on : ""} onClick={() => switchMode("advanced")}>
            🧩 Avanzado
          </button>
        </div>
        {isAdmin && (
          <>
            <button disabled={busy || !dirty} onClick={() => setSaveModal(true)}>
              💾 Guardar versión
            </button>
            <button className="primary" disabled={busy || clientErrors.length > 0} onClick={publish}>
              🚀 Publicar
            </button>
            <button onClick={() => setShowAI(true)} disabled={dirty} title={dirty ? "Guarda primero los cambios" : undefined}>
              ✨ Editar con IA
            </button>
          </>
        )}
        <button onClick={() => setShowVersions(true)}>🕘 Versiones</button>
        <button onClick={() => download(`flujo-${flow.id}.json`, definition)}>⬇️ Exportar</button>
        {isAdmin && (
          <>
            <button onClick={() => fileInput.current?.click()}>⬆️ Importar</button>
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) importFile(f);
                e.target.value = "";
              }}
            />
          </>
        )}
      </div>

      {notice && (
        <div className="inline small" role="status">
          <Badge tone="ok">✓</Badge> {notice}
          <button className="link small" onClick={() => setNotice(null)}>
            ocultar
          </button>
        </div>
      )}
      <ErrorBox error={error} />
      {orphans > 0 && mode === "advanced" && (
        <div className={styles.notice}>
          Hay {orphans} bloque(s) suelto(s) sin evento de inicio: no se guardan hasta que los conectes a un bloque de Eventos.
        </div>
      )}
      {allErrors.length > 0 && (
        <div className={styles.errors}>
          <strong>{allErrors.length} problema(s) por resolver</strong>
          <ul>
            {allErrors.slice(0, 8).map((e, i) => (
              <li key={i}>{e.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className={`${styles.body} ${mode === "advanced" ? "" : ""}`}>
        <div style={{ minWidth: 0 }}>
          {mode === "junior" ? (
            <>
              {!juniorOk && (
                <div className={styles.notice} style={{ marginBottom: 12 }}>
                  Este flujo usa bloques avanzados (operadores, IA, datos…). En Junior se muestra en solo lectura.
                  <button className="primary" onClick={() => switchMode("advanced")}>
                    Abrir en Avanzado
                  </button>
                </div>
              )}
              <JuniorEditor definition={definition} catalog={catalog} errors={blockErrors} readOnly={readOnly || !juniorOk} onChange={update} />
            </>
          ) : (
            <AdvancedEditor definition={definition} catalog={catalog} errors={blockErrors} readOnly={readOnly} onChange={update} />
          )}
        </div>
        <div className={styles.side}>
          <Simulator flowId={flow.id} definition={definition} />
          <VariablesPanel variables={definition.variables} readOnly={readOnly} onChange={(v) => update({ ...definition, variables: v })} />
        </div>
      </div>

      {saveModal && (
        <Modal
          title="Guardar versión"
          onClose={() => setSaveModal(false)}
          footer={
            <>
              <button onClick={() => setSaveModal(false)}>Cancelar</button>
              <button className="primary" disabled={busy} onClick={() => save(note)}>
                Guardar
              </button>
            </>
          }
        >
          <Field label="¿Qué cambiaste? (opcional)">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Ej. agregué la pregunta de presupuesto" autoFocus />
          </Field>
          <p className="small muted">Cada versión queda en el historial y se puede volver a cargar. Publicar usa la última versión guardada.</p>
        </Modal>
      )}
      {showVersions && (
        <VersionsDrawer
          flowId={flow.id}
          currentVersionId={flow.current_version_id}
          onClose={() => setShowVersions(false)}
          onLoad={(def, v) => {
            update(def);
            setNotice(`Cargada la versión v${v.version}: guarda para crear una nueva versión con este contenido`);
          }}
        />
      )}
      {showAI && (
        <JsonEditWithAI
          entityType="flow"
          entityId={flow.id}
          title={`Editar «${flow.name}» con IA`}
          onClose={() => setShowAI(false)}
          onApplied={() => {
            setShowAI(false);
            load();
            setNotice("La IA creó una versión nueva del flujo");
          }}
        />
      )}
    </div>
  );
}
