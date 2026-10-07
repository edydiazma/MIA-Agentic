"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { fmtDateTime, fmtNum, send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, PageHeader, Stat, Tabs, Toggle, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import {
  ENTRY_LABELS, GOAL_LABELS, STATUS_LABELS, STATUS_TONE,
  type Definition, type Enrollment, type JourneyDetail, type JourneyEntry, type JourneyMeta, type JourneyReport,
  type JourneySettings, type Segment, type SegmentField, type Step, type StepType,
} from "@/lib/journey-types";
import JourneyCanvas from "./JourneyCanvas";
import StepDrawer from "./StepDrawer";
import { errorsByStep, insertStep, removeStep, updateStep } from "./graph";
import css from "./journeys.module.css";

type Tab = "editor" | "config" | "report" | "enrollments";

export default function JourneyEditor({ journeyId }: { journeyId: number }) {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const detail = useApi<JourneyDetail>(`/api/journeys/${journeyId}`);
  const meta = useApi<JourneyMeta>("/api/journeys/meta");
  const fields = useApi<{ fields: SegmentField[] }>("/api/segments/fields");
  const report = useApi<JourneyReport>(`/api/journeys/${journeyId}/report`);
  const [tab, setTab] = useState<Tab>("editor");
  const [def, setDef] = useState<Definition | null>(null);
  const [entry, setEntry] = useState<JourneyEntry>({ type: "manual" });
  const [settings, setSettings] = useState<JourneySettings>({});
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [run, busy, actionError] = useAction();
  const [notice, setNotice] = useState<string | null>(null);

  const j = detail.data?.journey;
  useEffect(() => {
    if (!detail.data) return;
    setDef(detail.data.version?.definition ?? { start: "", steps: [] });
    setEntry(detail.data.journey.entry?.type ? detail.data.journey.entry : { type: "manual" });
    setSettings(detail.data.journey.settings ?? {});
    setName(detail.data.journey.name);
    setDescription(detail.data.journey.description ?? "");
    setErrors(detail.data.errors);
    setDirty(false);
  }, [detail.data]);

  // Validación en línea (con antirrebote) mientras se edita
  useEffect(() => {
    if (!def || !dirty) return;
    const t = setTimeout(async () => {
      if (!def.steps.length) return setErrors(["El journey no tiene pasos"]);
      try {
        setErrors((await send<{ errors: string[] }>("/api/journeys/validate", "POST", { definition: def })).errors);
      } catch {
        /* sin conexión: se valida al guardar */
      }
    }, 400);
    return () => clearTimeout(t);
  }, [def, dirty]);

  const { byStep, general } = useMemo(() => errorsByStep(errors), [errors]);
  const labels = (meta.data?.step_types ?? {}) as Record<string, string>;
  const step = def?.steps.find((s) => s.id === selected) ?? null;
  const editable = isAdmin && j?.status !== "archived";

  function edit(next: Definition) {
    setDef(next);
    setDirty(true);
  }

  async function save(): Promise<boolean> {
    if (!def) return false;
    const ok = await run(() => send(`/api/journeys/${journeyId}`, "PUT",
      { name: name.trim(), description: description.trim() || null, entry, settings, definition: def.steps.length ? def : null }));
    if (ok) {
      setNotice("Guardado como nueva versión.");
      await detail.reload();
    }
    return !!ok;
  }

  async function publish() {
    if (dirty && !(await save())) return;
    if (!window.confirm("¿Publicar esta versión? Los clientes nuevos entrarán con ella.")) return;
    const r = await run(() => send<{ enrolled: number }>(`/api/journeys/${journeyId}/publish`, "POST", {}));
    if (r) {
      setNotice(`Publicado.${r.enrolled ? ` ${r.enrolled} clientes inscritos.` : ""}`);
      detail.reload();
    }
  }

  async function action(a: "pause" | "resume" | "archive") {
    if (a === "archive" && !window.confirm("¿Archivar? Las inscripciones en curso terminan.")) return;
    if (await run(() => send(`/api/journeys/${journeyId}/${a}`, "POST"))) detail.reload();
  }

  if (detail.error) return <ErrorBox error={detail.error} />;
  if (!j || !def) return <Loading />;

  return (
    <>
      <PageHeader
        title={j.name}
        subtitle={<><Link href="/journeys">Journeys</Link> · <Badge tone={STATUS_TONE[j.status]}>{STATUS_LABELS[j.status]}</Badge>
          {" "}· {ENTRY_LABELS[j.entry?.type] ?? "—"} · versión {detail.data?.version?.version ?? "—"}</>}
        actions={isAdmin && (
          <>
            {editable && <button onClick={save} disabled={busy || !dirty}>Guardar</button>}
            {editable && <button className="primary" onClick={publish} disabled={busy || errors.length > 0}>Publicar</button>}
            {j.status === "active" && <button onClick={() => action("pause")} disabled={busy}>Pausar</button>}
            {j.status === "paused" && <button onClick={() => action("resume")} disabled={busy}>Reanudar</button>}
            {j.status !== "archived" && j.status !== "draft" && <button className="danger" onClick={() => action("archive")} disabled={busy}>Archivar</button>}
          </>
        )}
      />
      <ErrorBox error={actionError} />
      {notice && <div className="notice small">{notice}</div>}
      <Tabs value={tab} onChange={setTab}
        tabs={[["editor", "Pasos"], ["config", "Entrada y reglas"], ["report", "Reporte"], ["enrollments", "Inscripciones"]]} />

      {tab === "editor" && (
        <div className={css.layout}>
          <div className="stack">
            {general.length > 0 && (
              <div className={css.errors}><strong>Corrige antes de publicar</strong><ul>{general.map((e) => <li key={e}>{e}</li>)}</ul></div>
            )}
            <JourneyCanvas
              definition={def}
              labels={labels}
              entryLabel={ENTRY_LABELS[entry.type]}
              selected={selected}
              errors={byStep}
              report={report.data}
              readOnly={!editable}
              onSelect={setSelected}
              onInsert={(parent, branch, type: StepType) => {
                const [next, id] = insertStep(def, parent, branch, type);
                edit(next);
                setSelected(id);
              }}
            />
          </div>
          <div className={css.drawer}>
            {step && editable ? (
              <StepDrawer
                key={step.id}
                step={step}
                fields={fields.data?.fields ?? []}
                labels={labels}
                onChange={(patch: Partial<Step>) => edit(updateStep(def, step.id, patch))}
                onRemove={() => { edit(removeStep(def, step.id)); setSelected(null); }}
              />
            ) : (
              <Card><p className="muted small">Toca un paso para configurarlo o «+» para agregar uno. Las ramas de las condiciones y pruebas A/B se muestran lado a lado.</p></Card>
            )}
            {detail.data?.versions.length ? (
              <Card title="Versiones">
                <ul className="small">
                  {detail.data.versions.slice(0, 8).map((v) => (
                    <li key={v.id}>v{v.version} · {fmtDateTime(v.created_at)} {v.current && <Badge tone="ok">publicada</Badge>}</li>
                  ))}
                </ul>
              </Card>
            ) : null}
          </div>
        </div>
      )}

      {tab === "config" && (
        <ConfigPanel
          name={name} description={description} entry={entry} settings={settings} meta={meta.data}
          fields={fields.data?.fields ?? []} readOnly={!editable}
          onChange={(p) => {
            if (p.name !== undefined) setName(p.name);
            if (p.description !== undefined) setDescription(p.description);
            if (p.entry) setEntry(p.entry);
            if (p.settings) setSettings(p.settings);
            setDirty(true);
          }}
        />
      )}
      {tab === "report" && <ReportPanel report={report.data} error={report.error} onReload={report.reload} />}
      {tab === "enrollments" && <EnrollmentsPanel journeyId={journeyId} canEnroll={isAdmin && j.status === "active"} />}
    </>
  );
}

// --- Entrada y reglas ---------------------------------------------------------------------------------------------
function ConfigPanel({ name, description, entry, settings, meta, fields, readOnly, onChange }: {
  name: string; description: string; entry: JourneyEntry; settings: JourneySettings; meta: JourneyMeta | null;
  fields: SegmentField[]; readOnly: boolean;
  onChange: (p: { name?: string; description?: string; entry?: JourneyEntry; settings?: JourneySettings }) => void;
}) {
  const segments = useApi<Segment[]>("/api/segments");
  const setE = (p: Partial<JourneyEntry>) => onChange({ entry: { ...entry, ...p } });
  const setS = (p: Partial<JourneySettings>) => onChange({ settings: { ...settings, ...p } });
  const dateFields = fields.filter((f) => f.type === "date" || f.type === "ts");
  const quiet = settings.quiet_hours;
  const cap = settings.frequency_cap ?? {};
  const goal = settings.goal;
  const n = (v: string) => (v === "" ? null : Number(v));
  return (
    <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="grid-2" style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
        <Card title="Journey">
          <div className="stack">
            <Field label="Nombre"><input value={name} onChange={(e) => onChange({ name: e.target.value })} /></Field>
            <Field label="Descripción"><input value={description} onChange={(e) => onChange({ description: e.target.value })} /></Field>
          </div>
        </Card>
        <Card title="Entrada">
          <div className="stack">
            <Field label="¿Quién entra?">
              <select value={entry.type} onChange={(e) => onChange({ entry: { type: e.target.value as JourneyEntry["type"] } })}>
                {Object.entries(ENTRY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </Field>
            {(entry.type === "segment_enter" || entry.type === "segment_member" || entry.type === "date_field") && (
              <Field label={entry.type === "date_field" ? "Solo clientes de este segmento (opcional)" : "Segmento"}>
                <select value={entry.segment_id ?? ""} onChange={(e) => setE({ segment_id: n(e.target.value) })}>
                  <option value="">{entry.type === "date_field" ? "Todos" : "Selecciona…"}</option>
                  {segments.data?.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.member_count})</option>)}
                </select>
              </Field>
            )}
            {entry.type === "event" && (
              <>
                <Field label="Evento">
                  <select value={entry.event ?? ""} onChange={(e) => setE({ event: e.target.value || null })}>
                    <option value="">Selecciona…</option>
                    {Object.entries(meta?.events ?? {}).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </Field>
                <Field label="Filtro (opcional)" hint='JSON, p. ej. {"typification_id": 4} o {"pipeline": "ventas"}'>
                  <input defaultValue={entry.filter ? JSON.stringify(entry.filter) : ""} onBlur={(e) => {
                    try { setE({ filter: e.target.value.trim() ? JSON.parse(e.target.value) : {} }); } catch { /* se deja como estaba */ }
                  }} />
                </Field>
              </>
            )}
            {entry.type === "date_field" && (
              <>
                <Field label="Fecha del cliente">
                  <select value={entry.field ?? ""} onChange={(e) => setE({ field: e.target.value || null })}>
                    <option value="">Selecciona…</option>
                    {dateFields.map((f) => <option key={f.key} value={f.key}>{f.group} · {f.label}</option>)}
                  </select>
                </Field>
                <div className="inline">
                  <Field label="Días respecto a la fecha" hint="-30 = 30 días antes">
                    <input type="number" style={{ width: 100 }} value={entry.offset_days ?? 0} onChange={(e) => setE({ offset_days: Number(e.target.value) })} />
                  </Field>
                  <Field label="Hora"><input type="time" value={entry.at_time ?? "10:00"} onChange={(e) => setE({ at_time: e.target.value })} /></Field>
                </div>
              </>
            )}
            <Field label="Reingreso">
              <select value={settings.reentry ?? "never"} onChange={(e) => setS({ reentry: e.target.value as JourneySettings["reentry"] })}>
                <option value="never">Nunca (una vez por cliente)</option>
                <option value="after_days">Después de N días</option>
                <option value="always">Siempre que termine</option>
              </select>
            </Field>
            {settings.reentry === "after_days" && (
              <Field label="Días"><input type="number" min={1} value={settings.reentry_days ?? 30} onChange={(e) => setS({ reentry_days: n(e.target.value) })} /></Field>
            )}
          </div>
        </Card>
        <Card title="Límites">
          <div className="stack">
            <Toggle checked={!!quiet} onChange={(v) => setS({ quiet_hours: v ? { from: "20:00", to: "08:00" } : null })} label="Horas de silencio (los envíos se corren)" />
            {quiet && (
              <div className="inline">
                <Field label="Desde"><input type="time" value={quiet.from} onChange={(e) => setS({ quiet_hours: { ...quiet, from: e.target.value } })} /></Field>
                <Field label="Hasta"><input type="time" value={quiet.to} onChange={(e) => setS({ quiet_hours: { ...quiet, to: e.target.value } })} /></Field>
                <Field label="Zona horaria"><input placeholder="de la empresa" value={quiet.timezone ?? ""} onChange={(e) => setS({ quiet_hours: { ...quiet, timezone: e.target.value || null } })} /></Field>
              </div>
            )}
            <div className="inline">
              <Field label="Máx. mensajes por día" hint="Journeys + campañas">
                <input type="number" min={1} style={{ width: 90 }} value={cap.per_day ?? ""} onChange={(e) => setS({ frequency_cap: { ...cap, per_day: n(e.target.value) } })} />
              </Field>
              <Field label="Por semana">
                <input type="number" min={1} style={{ width: 90 }} value={cap.per_week ?? ""} onChange={(e) => setS({ frequency_cap: { ...cap, per_week: n(e.target.value) } })} />
              </Field>
            </div>
            <Toggle checked={settings.require_consent === "marketing"} onChange={(v) => setS({ require_consent: v ? "marketing" : null })}
              label="Exigir consentimiento de marketing" />
            <Field label="Prioridad de canales (mensaje automático)">
              <select value={(settings.channels_priority ?? ["whatsapp_cloud", "email", "webchat"]).join(",")}
                onChange={(e) => setS({ channels_priority: e.target.value.split(",") as JourneySettings["channels_priority"] })}>
                <option value="whatsapp_cloud,email,webchat">WhatsApp → correo → chat web</option>
                <option value="email,whatsapp_cloud,webchat">Correo → WhatsApp → chat web</option>
                <option value="whatsapp_cloud,webchat">WhatsApp → chat web</option>
                <option value="email">Solo correo</option>
              </select>
            </Field>
          </div>
        </Card>
        <Card title="Meta">
          <div className="stack">
            <Field label="El journey termina con éxito cuando…">
              <select value={goal?.event ?? ""} onChange={(e) => setS({ goal: e.target.value ? { ...(goal ?? {}), event: e.target.value } : null })}>
                <option value="">Sin meta</option>
                {(meta?.goals ?? []).map((g) => <option key={g} value={g}>{GOAL_LABELS[g] ?? g}</option>)}
              </select>
            </Field>
            {goal && (
              <>
                <Field label="Dentro de (días, opcional)"><input type="number" min={1} value={goal.window_days ?? ""} onChange={(e) => setS({ goal: { ...goal, window_days: n(e.target.value) } })} /></Field>
                {(goal.event === "stage" || goal.event === "deal_won") && (
                  <Field label="Pipeline"><input value={goal.pipeline ?? ""} onChange={(e) => setS({ goal: { ...goal, pipeline: e.target.value || null } })} /></Field>
                )}
                {goal.event === "stage" && (
                  <Field label="Etapa"><input value={goal.stage ?? ""} onChange={(e) => setS({ goal: { ...goal, stage: e.target.value || null } })} /></Field>
                )}
              </>
            )}
            <p className="small muted">Guarda los cambios con «Guardar» arriba.</p>
          </div>
        </Card>
      </div>
    </fieldset>
  );
}

// --- Reporte ------------------------------------------------------------------------------------------------------
const pct = (n: number | null | undefined) => (n == null ? "—" : `${(n * 100).toFixed(1)} %`);

function ReportPanel({ report, error, onReload }: { report: JourneyReport | null; error: string | null; onReload: () => void }) {
  if (error) return <ErrorBox error={error} />;
  if (!report) return <Loading />;
  const t = report.totals;
  const maxSent = Math.max(1, ...report.steps.map((s) => s.sent || s.branched || s.waited));
  return (
    <div className="stack">
      <div className="stats-row" style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
        <Stat label="Inscritos" value={fmtNum(t.enrolled ?? 0)} />
        <Stat label="En curso" value={fmtNum((t.active ?? 0) + (t.waiting ?? 0))} />
        <Stat label="Meta cumplida" value={fmtNum(t.goal_met ?? 0)} tone="ok"
          hint={t.enrolled ? pct((t.goal_met ?? 0) / t.enrolled) : undefined} />
        <Stat label="Completaron" value={fmtNum(t.completed ?? 0)} />
        <Stat label="Salieron" value={fmtNum(t.exited ?? 0)} />
        <Stat label="Fallaron" value={fmtNum(t.failed ?? 0)} tone={t.failed ? "bad" : undefined} />
      </div>
      <Card title="Embudo por paso" actions={<button onClick={onReload}>Actualizar</button>}>
        {!report.steps.length ? <Empty>Sin pasos.</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Paso</th><th /><th className="num">Enviados</th><th className="num">Entregados</th><th className="num">Leídos</th>
                  <th className="num">Respondieron</th><th className="num">Clics</th><th className="num">Saltados</th><th className="num">Fallidos</th>
                </tr>
              </thead>
              <tbody>
                {report.steps.map((s) => {
                  const base = s.sent || s.branched || s.waited;
                  return (
                    <tr key={s.id}>
                      <td><code>{s.id}</code> {s.label}</td>
                      <td><div className={css.funnelTrack}><div className={css.funnelBar} style={{ width: `${(base / maxSent) * 100}%` }} /></div></td>
                      <td className="num">{fmtNum(s.sent)}</td>
                      <td className="num">{fmtNum(s.delivered)} <span className="muted small">{s.sent ? pct(s.delivered / s.sent) : ""}</span></td>
                      <td className="num">{fmtNum(s.read)} <span className="muted small">{s.sent ? pct(s.read / s.sent) : ""}</span></td>
                      <td className="num">{fmtNum(s.replied)} <span className="muted small">{s.sent ? pct(s.replied / s.sent) : ""}</span></td>
                      <td className="num">{fmtNum(s.clicked)}</td>
                      <td className="num">{fmtNum(s.skipped)}</td>
                      <td className="num">{fmtNum(s.failed)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {report.ab.map((ab) => (
        <Card key={ab.step_id} title={`Prueba A/B · ${ab.step_id}`}
          actions={<span className="muted small">Conversión: {GOAL_LABELS[ab.metric] ?? (ab.metric === "goal_met" ? "meta cumplida" : ab.metric)}</span>}>
          <table className="table">
            <thead>
              <tr><th>Variante</th><th className="num">Clientes</th><th className="num">Conversiones</th><th className="num">Tasa</th>
                <th className="num">Lift vs {ab.variants[0]?.key}</th><th className="num">z</th><th className="num">p</th><th /></tr>
            </thead>
            <tbody>
              {ab.variants.map((v, i) => (
                <tr key={v.key}>
                  <td><strong>{v.key}</strong>{i === 0 && <span className="muted small"> (base)</span>}</td>
                  <td className="num">{fmtNum(v.enrolled)}</td>
                  <td className="num">{fmtNum(v.conversions)}</td>
                  <td className="num">{pct(v.rate)}</td>
                  <td className="num">{i === 0 ? "—" : pct(v.lift)}</td>
                  <td className="num">{v.z ?? "—"}</td>
                  <td className="num">{v.p_value ?? "—"}</td>
                  <td>{i > 0 && (v.significant ? <Badge tone="ok">Significativa (95 %)</Badge> : <Badge>Sin diferencia aún</Badge>)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}
    </div>
  );
}

// --- Inscripciones ------------------------------------------------------------------------------------------------
function EnrollmentsPanel({ journeyId, canEnroll }: { journeyId: number; canEnroll: boolean }) {
  const [status, setStatus] = useState("");
  const list = useApi<Enrollment[]>(`/api/journeys/${journeyId}/enrollments${status ? `?status=${status}` : ""}`);
  const [ids, setIds] = useState("");
  const [run, busy, error] = useAction();
  const [msg, setMsg] = useState<string | null>(null);

  async function enroll() {
    const contact_ids = ids.split(/[\s,;]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0);
    const r = await run(() => send<{ enrolled: number; skipped: number }>(`/api/journeys/${journeyId}/enroll`, "POST", { contact_ids }));
    if (r) {
      setMsg(`${r.enrolled} inscritos, ${r.skipped} omitidos (ya activos, reingreso no permitido o bloqueados).`);
      setIds("");
      list.reload();
    }
  }

  return (
    <Card title="Inscripciones" actions={
      <select value={status} onChange={(e) => setStatus(e.target.value)}>
        <option value="">Todas</option>
        {["active", "waiting", "completed", "goal_met", "exited", "failed"].map((s) => <option key={s} value={s}>{STATUS_LABELS[s]}</option>)}
      </select>
    }>
      {canEnroll && (
        <div className="inline" style={{ marginBottom: 12 }}>
          <input placeholder="IDs de clientes (separados por coma)" value={ids} onChange={(e) => setIds(e.target.value)} style={{ minWidth: 280 }} />
          <button onClick={enroll} disabled={busy || !ids.trim()}>Inscribir</button>
          {msg && <span className="small muted">{msg}</span>}
        </div>
      )}
      <ErrorBox error={error || list.error} />
      {list.loading && !list.data ? <Loading /> : !list.data?.length ? <Empty>Sin inscripciones.</Empty> : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Cliente</th><th>Estado</th><th>Paso</th><th>Variante</th><th>Próxima ejecución</th><th>Origen</th><th>Inscrito</th><th>Motivo de salida</th></tr>
            </thead>
            <tbody>
              {list.data.map((e) => (
                <tr key={`${e.id}-${e.enrolled_at}`}>
                  <td><Link href={`/clientes?c=${e.contact_id}`}>{e.contact_name ?? e.contact_phone ?? `#${e.contact_id}`}</Link></td>
                  <td><Badge tone={e.status === "goal_met" ? "ok" : e.status === "failed" ? "bad" : e.status === "waiting" ? "info" : "neutral"}>{STATUS_LABELS[e.status] ?? e.status}</Badge></td>
                  <td><code>{e.current_step ?? "—"}</code></td>
                  <td>{e.variant ?? "—"}</td>
                  <td>{fmtDateTime(e.next_run_at)}</td>
                  <td className="small">{e.source ?? "—"}</td>
                  <td>{fmtDateTime(e.enrolled_at)}</td>
                  <td className="small">{e.exit_reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
