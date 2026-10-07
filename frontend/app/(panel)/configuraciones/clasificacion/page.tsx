"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  send,
  type ClassifierSettings,
  type ConversationTag,
  type Group,
  type Settings,
} from "@/lib/api";
import { Card, ErrorBox, Field, Loading, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, useIsAdmin } from "@/components/config/common";
import ClassifierTest from "@/components/config/ClassifierTest";

type CortexLite = { id: number; name: string; purpose: string; is_active: boolean; members: { connection_name: string; model: string }[] };
const MODE_OPTIONS: [string, string][] = [
  ["auto", "Aplicar automáticamente"],
  ["suggest", "Solo sugerir al asesor"],
  ["off", "Desactivado"],
];
const FIELD_MODE_OPTIONS: [string, string][] = [
  ["fill_empty", "Llenar solo campos vacíos"],
  ["overwrite_ai", "Actualizar valores de la IA (nunca los de personas)"],
  ["suggest", "Solo sugerir"],
  ["off", "Desactivado"],
];

function GroupRow({ group, disabled, onSaved }: { group: Group; disabled: boolean; onSaved: () => void }) {
  const [desc, setDesc] = useState(group.description ?? "");
  const [run, busy, error] = useAction();
  const dirty = desc !== (group.description ?? "");
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="row">
        <strong>{group.name}</strong>
        {dirty && !disabled && (
          <button disabled={busy} onClick={() => run(async () => {
            await send(`/api/groups/${group.id}`, "PUT", { name: group.name, description: desc || null });
            onSaved();
          })}>
            {busy ? "Guardando…" : "Guardar descripción"}
          </button>
        )}
      </div>
      <textarea rows={2} disabled={disabled} value={desc} placeholder="Qué clientes o temas atiende este grupo"
                onChange={(e) => setDesc(e.target.value)} />
      <ErrorBox error={error} />
    </div>
  );
}

export default function ClasificacionPage() {
  const isAdmin = useIsAdmin();
  const { data: cortexes } = useApi<CortexLite[]>("/api/ai/cortexes");
  const { data, error: loadError } = useApi<ClassifierSettings>("/api/settings/classifier");
  const { data: convSettings } = useApi<Settings["conversations"]>("/api/settings/conversations");
  const { data: groups, reload: reloadGroups } = useApi<Group[]>("/api/groups");
  const { data: tagUsage } = useApi<ConversationTag[]>("/api/conversation-tags");
  const [draft, setDraft] = useState<ClassifierSettings | null>(null);
  const [run, busy, saveError] = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  if (!draft) return (
    <>
      <PageHeader title="Configuraciones" subtitle="Clasificación de conversaciones con IA." />
      <ConfigTabs />
      {loadError ? <ErrorBox error={loadError} /> : <Loading />}
    </>
  );

  const set = <K extends keyof ClassifierSettings>(k: K, v: ClassifierSettings[K]) => setDraft({ ...draft, [k]: v });
  const setMode = (k: keyof ClassifierSettings["modes"], v: string) =>
    setDraft({ ...draft, modes: { ...draft.modes, [k]: v } as ClassifierSettings["modes"] });
  const usage = Object.fromEntries((tagUsage ?? []).map((t) => [t.name, t.count]));
  const ro = !isAdmin;

  async function save() {
    const r = await run(() => send<ClassifierSettings>("/api/settings/classifier", "PUT", draft!));
    if (r) {
      setDraft(r);
      setSaved(new Date().toLocaleTimeString("es"));
    }
  }

  const updateTag = (i: number, patch: Partial<{ name: string; description: string }>) =>
    set("tags", draft.tags.map((t, j) => (j === i ? { ...t, ...patch } : t)));

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Clasificación con IA: etiquetas, tipificación, grupo y datos del cliente. Usa su propia conexión, distinta del bot."
      />
      <ConfigTabs />
      <AdminNotice />

      <div className="grid2" style={{ alignItems: "start" }}>
        <Card title="Conexión">
          <div className="form">
            <Toggle checked={draft.enabled} onChange={(v) => !ro && set("enabled", v)} label="Clasificación con IA activa" />
            <Field label="Cortex" hint="El Cortex decide qué modelo usar y hace failover si uno falla o tarda demasiado.">
              <select disabled={ro} value={draft.cortex_id ?? ""}
                      onChange={(e) => set("cortex_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">Cortex principal</option>
                {(cortexes ?? []).filter((c) => c.is_active).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.members.map((m) => m.model).join(" → ") || "sin conexiones"}
                  </option>
                ))}
              </select>
            </Field>
            <p className="small muted" style={{ margin: 0 }}>
              Las conexiones (Claude, OpenAI, compatibles) y sus claves se gestionan en{" "}
              <Link href="/automatizaciones/cortex/conexiones">Conexiones y failover</Link>.
            </p>
          </div>
        </Card>

        <Card title="Instrucciones del negocio">
          <p className="small muted" style={{ marginTop: 0 }}>
            Contexto y criterio general para el clasificador: tipo de negocio, qué cuenta como venta, cómo ser de conservador.
          </p>
          <textarea rows={10} disabled={ro} value={draft.instructions} onChange={(e) => set("instructions", e.target.value)} />
        </Card>
      </div>

      <Card title="Etiquetas" actions={!ro && (
        <button onClick={() => set("tags", [...draft.tags, { name: "", description: "" }])}>Agregar etiqueta</button>
      )}>
        <p className="small muted" style={{ marginTop: 0 }}>
          La descripción le dice a la IA cuándo usar cada etiqueta. Los nombres se guardan en minúscula.
        </p>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Nombre</th><th>Cuándo aplicarla</th><th className="num">En uso</th><th /></tr></thead>
            <tbody>
              {draft.tags.map((t, i) => (
                <tr key={i}>
                  <td style={{ width: 200 }}>
                    <input disabled={ro} value={t.name} placeholder="ej. reclamo" onChange={(e) => updateTag(i, { name: e.target.value })} />
                  </td>
                  <td><input disabled={ro} value={t.description} onChange={(e) => updateTag(i, { description: e.target.value })} /></td>
                  <td className="num">{usage[t.name.trim().toLowerCase()] ?? 0}</td>
                  <td className="right">
                    {!ro && <button className="icon" aria-label="Quitar etiqueta" onClick={() => set("tags", draft.tags.filter((_, j) => j !== i))}>✕</button>}
                  </td>
                </tr>
              ))}
              {draft.tags.length === 0 && <tr><td colSpan={4} className="muted">Sin etiquetas: la IA no etiquetará conversaciones.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid2" style={{ alignItems: "start", marginTop: 16 }}>
        <Card title="Tipificaciones">
          <p className="small muted" style={{ marginTop: 0 }}>
            Criterio para que la IA elija cada tipificación. Las opciones se editan en{" "}
            <Link href="/configuraciones/conversaciones">Configuraciones → Conversaciones</Link>.
          </p>
          <div className="stack">
            {(convSettings?.typifications ?? []).map((t) => (
              <Field key={t} label={t}>
                <textarea rows={2} disabled={ro} placeholder="Cuándo aplica (opcional)"
                          value={draft.typification_criteria[t] ?? ""}
                          onChange={(e) => set("typification_criteria", { ...draft.typification_criteria, [t]: e.target.value })} />
              </Field>
            ))}
          </div>
        </Card>

        <Card title="Enrutamiento por grupos">
          <p className="small muted" style={{ marginTop: 0 }}>
            La IA usa la descripción de cada grupo para decidir a cuál enviar la conversación. Se guarda al instante.
          </p>
          <div className="stack">
            {(groups ?? []).map((g) => <GroupRow key={g.id} group={g} disabled={ro} onSaved={reloadGroups} />)}
            {groups && groups.length === 0 && (
              <p className="muted small">No hay grupos. Créalos en <Link href="/configuraciones/usuarios">Gestión usuarios</Link>.</p>
            )}
          </div>
        </Card>
      </div>

      <div className="grid2" style={{ alignItems: "start", marginTop: 16 }}>
        <Card title="Cuándo analizar">
          <div className="form">
            <Toggle checked={draft.route_on_handoff} onChange={(v) => !ro && set("route_on_handoff", v)}
                    label="Al transferir a un asesor (elige el grupo antes de asignar)" />
            <Toggle checked={draft.classify_on_close} onChange={(v) => !ro && set("classify_on_close", v)}
                    label="Al cerrar la conversación" />
            <Field label="Re-analizar cada N mensajes del cliente" hint="0 = nunca.">
              <input type="number" min={0} disabled={ro} value={draft.every_n_messages}
                     onChange={(e) => set("every_n_messages", Number(e.target.value))} />
            </Field>
            <Field label="Mensajes recientes que se envían al modelo">
              <input type="number" min={5} max={500} disabled={ro} value={draft.max_messages}
                     onChange={(e) => set("max_messages", Number(e.target.value))} />
            </Field>
          </div>
        </Card>

        <Card title="Qué hacer con el resultado">
          <div className="form">
            {(["tags", "group", "typification"] as const).map((k) => (
              <Field key={k} label={{ tags: "Etiquetas", group: "Grupo", typification: "Tipificación" }[k]}
                     hint={k === "typification" ? "En automático solo se aplica al cerrar, si el asesor no eligió otra."
                       : k === "group" ? "En automático solo se aplica al transferir o en la cola sin asignar." : undefined}>
                <select disabled={ro} value={draft.modes[k]} onChange={(e) => setMode(k, e.target.value)}>
                  {MODE_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </Field>
            ))}
            <Field label="Campos del cliente" hint="La IA nunca sobrescribe un dato que editó una persona.">
              <select disabled={ro} value={draft.modes.fields} onChange={(e) => setMode("fields", e.target.value)}>
                {FIELD_MODE_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </Field>
            <Field label={`Confianza mínima: ${Math.round(draft.min_confidence * 100)} %`}
                   hint="Por debajo de este valor el resultado queda como sugerencia, no se aplica.">
              <input type="range" min={0} max={1} step={0.05} disabled={ro} value={draft.min_confidence}
                     onChange={(e) => set("min_confidence", Number(e.target.value))} />
            </Field>
          </div>
        </Card>
      </div>

      {isAdmin && (
        <div style={{ margin: "16px 0" }}>
          <SaveBar onSave={save} busy={busy} saved={saved} error={saveError} />
        </div>
      )}

      {isAdmin && <ClassifierTest draft={draft} />}
    </>
  );
}
