"use client";

import { useEffect, useState } from "react";
import { send } from "@/lib/api";
import type { AIAgentIn } from "@/lib/ai-types";
import {
  MATCH_CHANNELS,
  PORTALS,
  SECTION_LABEL,
  SECURITY_ACTIONS,
  type PipelineGroup,
  type PipelineStage,
  type RecoveryAttempt,
  type SourceRule,
  type TypificationDetailed,
  type TypificationSection,
} from "@/lib/agent-config-types";
import { Card, Empty, ErrorBox, Field, Tabs, Toggle, useAction, useApi } from "@/components/ui";

type Tab = "general" | "pauta" | "costos" | "etapas" | "etiquetas" | "tipificaciones" | "campos" | "recuperacion" | "seguridad";
type Setter = <K extends keyof AIAgentIn>(k: K, v: AIAgentIn[K]) => void;
type Group = { id: number; name: string };
type ContactField = { id: number; key: string; label: string; ai_extract: boolean; section?: string | null };

const TABS: [Tab, string][] = [
  ["general", "Instrucciones generales"],
  ["pauta", "Reconocimiento de pauta"],
  ["costos", "Optimización de costos"],
  ["etapas", "Etapas"],
  ["etiquetas", "Etiquetas"],
  ["tipificaciones", "Tipificaciones"],
  ["campos", "Campos a identificar"],
  ["recuperacion", "Recuperación por inactividad"],
  ["seguridad", "Reglas de seguridad"],
];
const TIMEZONES = ["America/Bogota", "America/Mexico_City", "America/Lima", "America/Santiago", "America/Argentina/Buenos_Aires",
  "America/Guayaquil", "America/Caracas", "America/Panama", "America/Costa_Rica", "America/Santo_Domingo", "America/New_York",
  "Europe/Madrid"];

/** Configuración global del agente (como en Atom): se guarda con el agente, salvo Etapas, Etiquetas y Tipificaciones,
 * que son de la empresa y tienen su propio botón de guardar. */
export default function AgentAdvancedConfig({ form, set, disabled, isAdmin }: {
  form: AIAgentIn;
  set: Setter;
  disabled: boolean;
  isAdmin: boolean;
}) {
  const [tab, setTab] = useState<Tab>("general");
  return (
    <Card title="Configuración global">
      <Tabs value={tab} onChange={setTab} tabs={TABS} />
      <div style={{ marginTop: 12 }}>
        {tab === "general" && <General form={form} set={set} disabled={disabled} />}
        {tab === "pauta" && <AdRecognition form={form} set={set} disabled={disabled} />}
        {tab === "costos" && <CostOptimization form={form} set={set} disabled={disabled} />}
        {tab === "etapas" && <StagesEditor isAdmin={isAdmin} />}
        {tab === "etiquetas" && <TagsEditor isAdmin={isAdmin} />}
        {tab === "tipificaciones" && <TypificationsEditor isAdmin={isAdmin} />}
        {tab === "campos" && <FieldsToExtract form={form} set={set} disabled={disabled} />}
        {tab === "recuperacion" && <Recovery form={form} set={set} disabled={disabled} />}
        {tab === "seguridad" && <Security form={form} set={set} disabled={disabled} />}
      </div>
    </Card>
  );
}

function General({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  return (
    <div className="form">
      <p className="muted small">
        El tono, el estilo y las reglas del agente van en «Instrucciones (prompt de sistema)», arriba. Aquí se definen
        los límites que el agente siempre respeta.
      </p>
      <div className="grid2">
        <Field label="Máximo de palabras por respuesta" hint="Vacío = sin límite explícito (Atom usa 300).">
          <input
            type="number"
            min={20}
            max={2000}
            value={form.max_words ?? ""}
            disabled={disabled}
            onChange={(e) => set("max_words", e.target.value ? Number(e.target.value) : null)}
          />
        </Field>
        <Field label="Zona horaria" hint="Vacío = la de la empresa. Se usa para fechas, horarios y saludos.">
          <select value={form.timezone ?? ""} disabled={disabled} onChange={(e) => set("timezone", e.target.value || null)}>
            <option value="">La de la empresa</option>
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
        </Field>
      </div>
    </div>
  );
}

const EMPTY_RULE: SourceRule = {
  name: "",
  enabled: true,
  match: { portal: "any" },
  actions: { tags: [], skip_intent_question: true, handoff: false },
};

function AdRecognition({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  const groups = useApi<Group[]>("/api/groups");
  const stages = useApi<PipelineGroup[]>("/api/pipeline-stages");
  const rules = form.source_rules ?? [];
  const update = (i: number, r: SourceRule) => set("source_rules", rules.map((x, j) => (j === i ? r : x)));
  const remove = (i: number) => set("source_rules", rules.filter((_x, j) => j !== i));
  const stageOptions = (stages.data ?? []).flatMap((p) => p.stages.map((s) => [`${p.pipeline}:${s.key}`, `${p.label} · ${s.name}`]));

  return (
    <div className="form">
      <Toggle
        checked={form.ad_context_enabled}
        onChange={(v) => !disabled && set("ad_context_enabled", v)}
        label="Personalizar el primer mensaje con el anuncio, la publicación o el enlace de origen"
      />
      <p className="muted small">
        El agente recibe el canal, la campaña, el anuncio (título y texto), si llegó por una publicación, el mensaje
        disparador y el portal de clasificados (MercadoLibre, TuCarro…) solo en su primer mensaje o cuando el cliente
        vuelve por otro anuncio.
      </p>
      <Field label="Cómo debe usar el origen" hint="Ej.: si llega por un portal de usados, no preguntes el motivo: ya se sabe.">
        <textarea
          rows={4}
          value={form.ad_context_prompt ?? ""}
          disabled={disabled}
          onChange={(e) => set("ad_context_prompt", e.target.value || null)}
        />
      </Field>

      <h4 style={{ margin: "8px 0 0" }}>Reglas por fuente</h4>
      <p className="muted small">Cuando llega un cliente que cumple la condición, se aplican las acciones (todas las condiciones deben cumplirse).</p>
      {rules.length === 0 && <Empty>Sin reglas. Ej.: «Portales → grupo Recuperación usados, etiqueta ML,TuCarro».</Empty>}
      {rules.map((r, i) => (
        <div key={i} className="card" style={{ padding: 12 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <input
              aria-label="Nombre de la regla"
              placeholder="Nombre de la regla"
              value={r.name}
              disabled={disabled}
              onChange={(e) => update(i, { ...r, name: e.target.value })}
              style={{ maxWidth: 320 }}
            />
            <div className="inline">
              <Toggle checked={r.enabled} onChange={(v) => !disabled && update(i, { ...r, enabled: v })} label="Activa" />
              {!disabled && <button className="danger" onClick={() => remove(i)}>Eliminar</button>}
            </div>
          </div>
          <div className="grid2" style={{ marginTop: 8 }}>
            <div className="stack" style={{ gap: 6 }}>
              <strong className="small">Si…</strong>
              <Field label="Canal de llegada">
                <select value={r.match.channel ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, channel: e.target.value || null } })}>
                  <option value="">Cualquiera</option>
                  {MATCH_CHANNELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </Field>
              <Field label="Portal de clasificados">
                <select value={r.match.portal ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, portal: e.target.value || null } })}>
                  <option value="">No aplica</option>
                  {PORTALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </Field>
              <Field label="Origen">
                <select value={r.match.source_type ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, source_type: (e.target.value || null) as "ad" | "post" | null } })}>
                  <option value="">Anuncio o publicación</option>
                  <option value="ad">Solo anuncio</option>
                  <option value="post">Solo publicación</option>
                </select>
              </Field>
              <Field label="La campaña contiene">
                <input value={r.match.campaign_contains ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, campaign_contains: e.target.value || null } })} />
              </Field>
              <Field label="ID del anuncio">
                <input value={r.match.ad_id ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, ad_id: e.target.value || null } })} />
              </Field>
              <Field label="El mensaje contiene">
                <input value={r.match.text_contains ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, match: { ...r.match, text_contains: e.target.value || null } })} />
              </Field>
            </div>
            <div className="stack" style={{ gap: 6 }}>
              <strong className="small">Entonces…</strong>
              <Field label="Enviar al grupo">
                <select value={r.actions.group_id ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, actions: { ...r.actions, group_id: e.target.value ? Number(e.target.value) : null, group: null } })}>
                  <option value="">Sin cambio</option>
                  {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
              </Field>
              <Field label="Etiquetas" hint="Separadas por coma.">
                <input value={(r.actions.tags ?? []).join(", ")} disabled={disabled}
                  onChange={(e) => update(i, { ...r, actions: { ...r.actions, tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) } })} />
              </Field>
              <Field label="Marcar etapa">
                <select value={r.actions.stage ? `${r.actions.stage.pipeline}:${r.actions.stage.key}` : ""} disabled={disabled}
                  onChange={(e) => {
                    const [pipeline, key] = e.target.value.split(":");
                    update(i, { ...r, actions: { ...r.actions, stage: e.target.value ? { pipeline, key } : null } });
                  }}>
                  <option value="">Ninguna</option>
                  {stageOptions.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </Field>
              <Toggle checked={r.actions.skip_intent_question} onChange={(v) => !disabled && update(i, { ...r, actions: { ...r.actions, skip_intent_question: v } })}
                label="No preguntar el motivo (ya se conoce)" />
              <Toggle checked={r.actions.handoff} onChange={(v) => !disabled && update(i, { ...r, actions: { ...r.actions, handoff: v } })}
                label="Transferir de inmediato a un asesor" />
              <Field label="Instrucción extra para el agente">
                <input value={r.actions.note ?? ""} disabled={disabled}
                  onChange={(e) => update(i, { ...r, actions: { ...r.actions, note: e.target.value || null } })} />
              </Field>
            </div>
          </div>
        </div>
      ))}
      {!disabled && (
        <div>
          <button onClick={() => set("source_rules", [...rules, { ...EMPTY_RULE, name: `Regla ${rules.length + 1}` }])}>+ Agregar regla</button>
        </div>
      )}
    </div>
  );
}

function CostOptimization({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  return (
    <div className="form">
      <Toggle checked={form.cost_optimization} onChange={(v) => !disabled && set("cost_optimization", v)}
        label="Reducir los mensajes que Meta factura" />
      <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
        <li>La recuperación por inactividad solo escribe dentro de la ventana de atención (24 h desde el último mensaje del cliente; 72 h si llegó por un anuncio Click to WhatsApp): fuera de ella haría falta una plantilla de pago.</li>
        <li>Los webhooks que envían plantillas pueden mandar el mismo texto como mensaje libre cuando la ventana está abierta (opción «preferir mensaje libre» del webhook).</li>
        <li>Al transferir a un asesor, el bot no envía un mensaje extra si ya respondió.</li>
      </ul>
      <p className="muted small">Según las tarifas de Meta por mensaje, los mensajes de servicio dentro de la ventana no se cobran; las plantillas de marketing sí.</p>
    </div>
  );
}

function FieldsToExtract({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  const fields = useApi<ContactField[]>("/api/contact-fields");
  const [name, setName] = useState("");
  const [run, busy, error] = useAction();
  const selected = form.extract_field_ids ?? [];
  const toggle = (id: number) => set("extract_field_ids", selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);

  async function create() {
    const label = name.trim();
    if (!label) return;
    const key = label.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^([^a-z])/, "f_$1").slice(0, 50);
    const f = await run(() => send<ContactField>("/api/contact-fields", "POST", { key, label, type: "text", ai_extract: true }));
    if (f) {
      setName("");
      await fields.reload();
      set("extract_field_ids", [...selected, f.id]);
    }
  }

  return (
    <div className="form">
      <p className="muted small">Qué información debe identificar y guardar el agente si el cliente la menciona. Ninguno marcado = todos los campos con extracción por IA.</p>
      {(fields.data ?? []).length === 0 ? (
        <Empty>No hay campos de cliente todavía.</Empty>
      ) : (
        <div className="grid2">
          {(fields.data ?? []).map((f) => (
            <label key={f.id} className="inline">
              <input type="checkbox" checked={selected.includes(f.id)} disabled={disabled} onChange={() => toggle(f.id)} />
              <span>{f.label}</span>
              {!f.ai_extract && <span className="muted small">(sin extracción IA)</span>}
            </label>
          ))}
        </div>
      )}
      {!disabled && (
        <div className="row">
          <input aria-label="Nuevo campo" placeholder="Nombre del campo (ej. Medio de contacto)" value={name} onChange={(e) => setName(e.target.value)} />
          <button disabled={busy || !name.trim()} onClick={create}>Crear campo</button>
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

function Recovery({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  const typs = useApi<TypificationDetailed[]>("/api/typifications/detailed");
  const attempts = form.recovery_attempts ?? [];
  const update = (i: number, a: RecoveryAttempt) => set("recovery_attempts", attempts.map((x, j) => (j === i ? a : x)));
  return (
    <div className="form">
      <Toggle checked={form.recovery_enabled} onChange={(v) => !disabled && set("recovery_enabled", v)}
        label="Intentar recuperar al cliente cuando deja de responder" />
      <p className="muted small">Hasta 3 intentos (máximo 12 horas cada uno, contados desde el intento anterior). Con «usar IA» el agente escribe el mensaje con todo el contexto de la conversación usando tu texto como guía.</p>
      {attempts.map((a, i) => (
        <div key={i} className="row" style={{ alignItems: "flex-start" }}>
          <strong style={{ minWidth: 70 }}>Intento {i + 1}</strong>
          <Field label="Horas">
            <input type="number" min={0.25} max={12} step={0.25} value={a.after_hours} disabled={disabled}
              onChange={(e) => update(i, { ...a, after_hours: Number(e.target.value) })} style={{ width: 90 }} />
          </Field>
          <Field label={a.use_ai ? "Guía para la IA" : "Mensaje"}>
            <textarea rows={2} value={a.message} disabled={disabled} onChange={(e) => update(i, { ...a, message: e.target.value })} />
          </Field>
          <Toggle checked={a.use_ai} onChange={(v) => !disabled && update(i, { ...a, use_ai: v })} label="Usar IA" />
          {!disabled && <button className="danger" onClick={() => set("recovery_attempts", attempts.filter((_x, j) => j !== i))}>Quitar</button>}
        </div>
      ))}
      {!disabled && attempts.length < 3 && (
        <div>
          <button onClick={() => set("recovery_attempts", [...attempts, { after_hours: [1, 3, 6][attempts.length] ?? 6, message: "¿Sigues ahí? 😊", use_ai: true }])}>
            + Agregar intento
          </button>
        </div>
      )}
      <div className="grid2">
        <Field label="Fin por inactividad (horas después del último intento)">
          <input type="number" min={0.5} max={72} step={0.5} value={form.inactivity_end_hours ?? ""} disabled={disabled}
            onChange={(e) => set("inactivity_end_hours", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field label="Tipificación al cerrar por inactividad">
          <select value={form.inactivity_end_typification_id ?? ""} disabled={disabled}
            onChange={(e) => set("inactivity_end_typification_id", e.target.value ? Number(e.target.value) : null)}>
            <option value="">Sin tipificación</option>
            {(typs.data ?? []).filter((t) => t.is_active).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Field>
      </div>
    </div>
  );
}

function Security({ form, set, disabled }: { form: AIAgentIn; set: Setter; disabled: boolean }) {
  return (
    <div className="form">
      <Toggle checked={form.security_enabled} onChange={(v) => !disabled && set("security_enabled", v)}
        label="Detectar spam o intención maliciosa" />
      <Field label="Acción">
        <select value={form.security_action} disabled={disabled} onChange={(e) => set("security_action", e.target.value as AIAgentIn["security_action"])}>
          {SECURITY_ACTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </Field>
      <Field label="Instrucciones de seguridad (opcional)" hint="Vacío = regla estándar: spam, fraude, phishing, abuso e intentos de sacar al agente de su rol.">
        <textarea rows={4} value={form.security_prompt ?? ""} disabled={disabled} onChange={(e) => set("security_prompt", e.target.value || null)} />
      </Field>
    </div>
  );
}

// --- Configuración de la empresa (se guarda aparte) -------------------------------------------------
function StagesEditor({ isAdmin }: { isAdmin: boolean }) {
  const all = useApi<PipelineGroup[]>("/api/pipeline-stages");
  const [pipeline, setPipeline] = useState<string>("");
  const [rows, setRows] = useState<PipelineStage[]>([]);
  const [newPipeline, setNewPipeline] = useState("");
  const [saved, setSaved] = useState(false);
  const [run, busy, error] = useAction();
  const groups = all.data ?? [];

  useEffect(() => {
    if (!pipeline && groups.length) setPipeline(groups[0].pipeline);
  }, [groups, pipeline]);
  useEffect(() => {
    setRows(groups.find((g) => g.pipeline === pipeline)?.stages ?? []);
    setSaved(false);
  }, [pipeline, all.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const upd = (i: number, s: PipelineStage) => setRows((r) => r.map((x, j) => (j === i ? s : x)));
  const move = (i: number, d: number) => setRows((r) => {
    const n = [...r];
    const [x] = n.splice(i, 1);
    n.splice(Math.max(0, Math.min(n.length, i + d)), 0, x);
    return n;
  });

  async function save() {
    const ok = await run(() => send(`/api/pipelines/${pipeline}/stages`, "PUT", { stages: rows }));
    if (ok) {
      setSaved(true);
      await all.reload();
    }
  }
  async function defaults() {
    if (await run(() => send("/api/pipeline-stages/defaults", "POST", {}))) await all.reload();
  }

  return (
    <div className="form">
      <p className="muted small">Etapas por línea de negocio (ej. Nuevos: Lead → MQL → SQL). La IA marca una etapa cuando se cumple su condición y mueve la oportunidad del cliente; queda el historial. Son de la empresa: aplican a todos los agentes.</p>
      <div className="row">
        <select aria-label="Línea de negocio" value={pipeline} onChange={(e) => setPipeline(e.target.value)}>
          {groups.map((g) => <option key={g.pipeline} value={g.pipeline}>{g.label}</option>)}
          {pipeline && !groups.some((g) => g.pipeline === pipeline) && <option value={pipeline}>{pipeline}</option>}
        </select>
        {isAdmin && (
          <>
            <input aria-label="Nueva línea" placeholder="nueva línea (ej. usados)" value={newPipeline} onChange={(e) => setNewPipeline(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} style={{ maxWidth: 200 }} />
            <button disabled={!newPipeline} onClick={() => { setPipeline(newPipeline); setRows([]); setNewPipeline(""); }}>Crear línea</button>
            {groups.length === 0 && <button onClick={defaults} disabled={busy}>Usar etapas sugeridas</button>}
          </>
        )}
      </div>
      {pipeline && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Clave</th><th>Nombre</th><th>Nombre en el CRM / Atom</th><th>Condición para la IA</th><th>Tipo</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((s, i) => (
                <tr key={i}>
                  <td><input aria-label="Clave" value={s.key} disabled={!isAdmin || Boolean(s.id)} onChange={(e) => upd(i, { ...s, key: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "") })} style={{ width: 110 }} /></td>
                  <td><input aria-label="Nombre" value={s.name} disabled={!isAdmin} onChange={(e) => upd(i, { ...s, name: e.target.value })} /></td>
                  <td><input aria-label="Nombre externo" value={s.external_name ?? ""} disabled={!isAdmin} onChange={(e) => upd(i, { ...s, external_name: e.target.value || null })} /></td>
                  <td><textarea aria-label="Condición" rows={2} value={s.ai_condition ?? ""} disabled={!isAdmin} placeholder="Vacío = solo manual" onChange={(e) => upd(i, { ...s, ai_condition: e.target.value || null })} /></td>
                  <td>
                    <select aria-label="Tipo" value={s.is_won ? "won" : s.is_lost ? "lost" : "open"} disabled={!isAdmin}
                      onChange={(e) => upd(i, { ...s, is_won: e.target.value === "won", is_lost: e.target.value === "lost" })}>
                      <option value="open">Abierta</option><option value="won">Ganada</option><option value="lost">Perdida</option>
                    </select>
                  </td>
                  <td className="inline">
                    {isAdmin && (
                      <>
                        <button className="icon" aria-label="Subir" onClick={() => move(i, -1)}>↑</button>
                        <button className="icon" aria-label="Bajar" onClick={() => move(i, 1)}>↓</button>
                        <button className="icon" aria-label="Quitar" onClick={() => setRows((r) => r.filter((_x, j) => j !== i))}>✕</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {isAdmin && pipeline && (
        <div className="row">
          <button onClick={() => setRows((r) => [...r, { key: `etapa_${r.length + 1}`, name: "", external_name: null, ai_condition: null, probability: null, is_won: false, is_lost: false, is_active: true }])}>+ Agregar etapa</button>
          <button className="primary" disabled={busy || rows.length === 0 || rows.some((s) => !s.key || !s.name.trim())} onClick={save}>Guardar etapas</button>
          {saved && <span className="muted small">Guardado</span>}
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

type ClassifierTag = { name: string; description: string };

function TagsEditor({ isAdmin }: { isAdmin: boolean }) {
  const settings = useApi<{ tags: ClassifierTag[] }>("/api/settings/classifier");
  const [tags, setTags] = useState<ClassifierTag[]>([]);
  const [saved, setSaved] = useState(false);
  const [run, busy, error] = useAction();
  useEffect(() => setTags(settings.data?.tags ?? []), [settings.data]);
  const upd = (i: number, t: ClassifierTag) => { setTags((l) => l.map((x, j) => (j === i ? t : x))); setSaved(false); };
  async function save() {
    const clean = tags.map((t) => ({ name: t.name.trim().toLowerCase(), description: t.description.trim() })).filter((t) => t.name);
    if (await run(() => send("/api/settings/classifier", "PUT", { tags: clean }))) {
      setSaved(true);
      await settings.reload();
    }
  }
  return (
    <div className="form">
      <p className="muted small">La IA aplica cada etiqueta cuando se cumple su condición (también se usa en la clasificación automática). Son de la empresa.</p>
      {tags.map((t, i) => (
        <div key={i} className="row" style={{ alignItems: "flex-start" }}>
          <Field label="Etiqueta"><input value={t.name} disabled={!isAdmin} onChange={(e) => upd(i, { ...t, name: e.target.value })} style={{ width: 180 }} /></Field>
          <Field label="Condición para aplicar"><textarea rows={2} value={t.description} disabled={!isAdmin} onChange={(e) => upd(i, { ...t, description: e.target.value })} /></Field>
          {isAdmin && <button className="danger" onClick={() => setTags((l) => l.filter((_x, j) => j !== i))}>Quitar</button>}
        </div>
      ))}
      {isAdmin && (
        <div className="row">
          <button onClick={() => setTags((l) => [...l, { name: "", description: "" }])}>+ Crear etiqueta</button>
          <button className="primary" disabled={busy} onClick={save}>Guardar etiquetas</button>
          {saved && <span className="muted small">Guardado</span>}
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}

const EMPTY_TYP = { name: "", criteria: "", is_success: false, is_active: true, section: "followup" as TypificationSection, keyword: "", group_ids: [] as number[], reactivate_bot_after_h: null as number | null, required_fields: [] as string[] };

function TypificationsEditor({ isAdmin }: { isAdmin: boolean }) {
  const typs = useApi<TypificationDetailed[]>("/api/typifications/detailed");
  const groups = useApi<Group[]>("/api/groups");
  const [editing, setEditing] = useState<(typeof EMPTY_TYP & { id?: number }) | null>(null);
  const [run, busy, error] = useAction();

  async function save() {
    if (!editing) return;
    const body = { ...editing, keyword: editing.keyword || null, criteria: editing.criteria || null };
    const ok = await run(() => editing.id ? send(`/api/typifications/${editing.id}`, "PUT", body) : send("/api/typifications", "POST", body));
    if (ok) {
      setEditing(null);
      await typs.reload();
    }
  }

  return (
    <div className="form">
      <p className="muted small">La condición guía a la IA para tipificar. «Reactivar bot» deja el bot en pausa N horas tras tipificar (si el cliente responde antes, vuelve al asesor). Los datos obligatorios se piden al cerrar (ej. deal.amount, deal.currency, field:factura).</p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Tipificación</th><th>Sección</th><th>Palabra clave</th><th>Condición</th><th>Reactivar bot</th><th>Obligatorios</th><th /></tr></thead>
          <tbody>
            {(typs.data ?? []).filter((t) => t.is_active).map((t) => (
              <tr key={t.id}>
                <td><strong>{t.name}</strong>{t.is_success && <span className="muted small"> · venta</span>}</td>
                <td>{t.section ? SECTION_LABEL[t.section] : "—"}</td>
                <td>{t.keyword ?? "—"}</td>
                <td className="small">{t.criteria ?? "—"}</td>
                <td>{t.reactivate_bot_after_h ? `${t.reactivate_bot_after_h} h` : "—"}</td>
                <td className="small">{t.required_fields.join(", ") || "—"}</td>
                <td>{isAdmin && <button onClick={() => setEditing({ ...EMPTY_TYP, ...t, criteria: t.criteria ?? "", keyword: t.keyword ?? "", section: t.section ?? "neutral" })}>Editar</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {isAdmin && !editing && <div><button onClick={() => setEditing({ ...EMPTY_TYP })}>+ Agregar tipificación</button></div>}
      {editing && (
        <div className="card" style={{ padding: 12 }}>
          <div className="grid2">
            <Field label="Nombre"><input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></Field>
            <Field label="Sección">
              <select value={editing.section} onChange={(e) => setEditing({ ...editing, section: e.target.value as TypificationSection })}>
                {(Object.keys(SECTION_LABEL) as TypificationSection[]).map((k) => <option key={k} value={k}>{SECTION_LABEL[k]}</option>)}
              </select>
            </Field>
            <Field label="Palabra clave (única)"><input value={editing.keyword} onChange={(e) => setEditing({ ...editing, keyword: e.target.value })} /></Field>
            <Field label="Reactivar bot después de (horas)">
              <input type="number" min={0.5} step={0.5} value={editing.reactivate_bot_after_h ?? ""} onChange={(e) => setEditing({ ...editing, reactivate_bot_after_h: e.target.value ? Number(e.target.value) : null })} />
            </Field>
          </div>
          <Field label="Condición para guardar (guía para la IA)"><textarea rows={3} value={editing.criteria} onChange={(e) => setEditing({ ...editing, criteria: e.target.value })} /></Field>
          <Field label="Datos obligatorios al cerrar" hint="Separados por coma: deal.amount, deal.currency, field:factura">
            <input value={editing.required_fields.join(", ")} onChange={(e) => setEditing({ ...editing, required_fields: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
          </Field>
          <Field label="Disponible para los grupos (vacío = todos)">
            <div className="inline" style={{ flexWrap: "wrap" }}>
              {(groups.data ?? []).map((g) => (
                <label key={g.id} className="inline">
                  <input type="checkbox" checked={editing.group_ids.includes(g.id)} onChange={() => setEditing({ ...editing, group_ids: editing.group_ids.includes(g.id) ? editing.group_ids.filter((x) => x !== g.id) : [...editing.group_ids, g.id] })} />
                  {g.name}
                </label>
              ))}
            </div>
          </Field>
          <Toggle checked={editing.is_success} onChange={(v) => setEditing({ ...editing, is_success: v })} label="Cuenta como venta" />
          <div className="row">
            <button className="primary" disabled={busy || !editing.name.trim()} onClick={save}>Guardar</button>
            <button onClick={() => setEditing(null)}>Cancelar</button>
          </div>
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}
