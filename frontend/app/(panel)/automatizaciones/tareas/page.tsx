"use client";

import { useState } from "react";
import { send, type Automation, type AutomationType, type Group, type Settings } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, DAY_LABELS, DaysPicker, TIMEZONES, useIsAdmin } from "@/components/config/common";

const TYPES: Record<AutomationType, { label: string; help: string }> = {
  welcome: { label: "Bienvenida", help: "Envía un mensaje la primera vez que un contacto nuevo escribe; luego responde el bot." },
  keyword_reply: { label: "Respuesta por palabra clave", help: "Si el mensaje contiene una palabra clave, responde un texto fijo y el bot de IA no contesta." },
  keyword_handoff: { label: "Transferir por palabra clave", help: "Si el mensaje contiene una palabra clave, pasa la conversación a un asesor (opcionalmente a un grupo)." },
  business_hours: { label: "Horario de atención", help: "Si la conversación se transfiere a un asesor fuera del horario, avisa al cliente." },
  inactivity_close: { label: "Cierre por inactividad", help: "Cierra las conversaciones sin mensajes después de X horas (con mensaje opcional)." },
};

type Draft = { id?: number; name: string; type: AutomationType; config: Record<string, any>; enabled: boolean; priority: number };

const emptyConfig = (t: AutomationType, tz: string): Record<string, any> =>
  ({
    welcome: { message: "" },
    keyword_reply: { keywords: [], match: "contains", reply: "" },
    keyword_handoff: { keywords: [], match: "contains", message: "", group_id: null },
    business_hours: { timezone: tz, days: [0, 1, 2, 3, 4], start: "08:00", end: "18:00", message: "" },
    inactivity_close: { hours: 24, message: "" },
  })[t];

function summary(a: Automation, groups: Group[]): string {
  const c = a.config || {};
  switch (a.type) {
    case "welcome":
      return `«${c.message ?? ""}»`;
    case "keyword_reply":
      return `${(c.keywords ?? []).join(", ")} → «${c.reply ?? ""}»`;
    case "keyword_handoff": {
      const g = groups.find((x) => x.id === c.group_id);
      return `${(c.keywords ?? []).join(", ")} → asesor${g ? ` (${g.name})` : ""}`;
    }
    case "business_hours":
      return `${(c.days ?? []).map((d: number) => DAY_LABELS[d]).join(", ")} · ${c.start}–${c.end} (${c.timezone || "zona de la empresa"})`;
    case "inactivity_close":
      return `Tras ${c.hours} h sin mensajes`;
  }
}

export default function TareasPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<Automation[]>("/api/automations");
  const groups = useApi<Group[]>("/api/groups").data ?? [];
  const company = useApi<Settings["company"]>("/api/settings/company").data;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();

  const tz = company?.timezone ?? "America/Bogota";

  function openNew() {
    setActionError(null);
    setDraft({ name: "", type: "welcome", config: emptyConfig("welcome", tz), enabled: true, priority: 100 });
  }

  async function toggle(a: Automation, enabled: boolean) {
    await run(() => send(`/api/automations/${a.id}`, "PUT", { ...a, enabled }));
    reload();
  }

  async function remove(a: Automation) {
    if (!confirm(`¿Eliminar la tarea «${a.name}»?`)) return;
    await run(() => send(`/api/automations/${a.id}`, "DELETE"));
    reload();
  }

  async function save() {
    if (!draft) return;
    const { id, ...body } = draft;
    const ok = await run(() => (id ? send(`/api/automations/${id}`, "PUT", body) : send("/api/automations", "POST", body)));
    if (ok) {
      setDraft(null);
      reload();
    }
  }

  const setCfg = (k: string, v: any) => setDraft((d) => (d ? { ...d, config: { ...d.config, [k]: v } } : d));

  return (
    <>
      <PageHeader
        title="Tareas automatizadas"
        subtitle="Reglas que se ejecutan antes que el agente de IA, en orden de prioridad."
        actions={isAdmin && <button className="primary" onClick={openNew}>Nueva tarea</button>}
      />
      <AdminNotice />
      <ErrorBox error={error || (!draft ? actionError : null)} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Card><Empty>Aún no hay tareas automatizadas.</Empty></Card>
      ) : (
        <div className="stack">
          {data.map((a) => (
            <Card key={a.id}>
              <div className="row">
                <div className="stack" style={{ gap: 4, minWidth: 0 }}>
                  <div className="inline">
                    <strong>{a.name}</strong>
                    <Badge tone="info">{TYPES[a.type]?.label ?? a.type}</Badge>
                    <span className="muted small">Prioridad {a.priority}</span>
                  </div>
                  <span className="muted small">{summary(a, groups)}</span>
                </div>
                <div className="inline">
                  <Toggle checked={a.enabled} onChange={(v) => isAdmin && toggle(a, v)} label={a.enabled ? "Activa" : "Inactiva"} />
                  {isAdmin && (
                    <>
                      <button onClick={() => { setActionError(null); setDraft({ ...a, config: { ...a.config } }); }}>Editar</button>
                      <button className="danger" onClick={() => remove(a)} disabled={busy}>Eliminar</button>
                    </>
                  )}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {draft && (
        <Modal
          title={draft.id ? "Editar tarea" : "Nueva tarea"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.name.trim()}>Guardar</button>
            </>
          }
        >
          <Field label="Nombre">
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </Field>
          <Field label="Tipo" hint={TYPES[draft.type].help}>
            <select
              value={draft.type}
              disabled={!!draft.id}
              onChange={(e) => {
                const t = e.target.value as AutomationType;
                setDraft({ ...draft, type: t, config: emptyConfig(t, tz) });
              }}
            >
              {(Object.keys(TYPES) as AutomationType[]).map((t) => (
                <option key={t} value={t}>{TYPES[t].label}</option>
              ))}
            </select>
          </Field>

          {(draft.type === "keyword_reply" || draft.type === "keyword_handoff") && (
            <>
              <Field label="Palabras clave" hint="Separadas por comas. No distingue mayúsculas ni tildes.">
                <input
                  value={(draft.config.keywords ?? []).join(", ")}
                  onChange={(e) => setCfg("keywords", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
                />
              </Field>
              <Field label="Coincidencia">
                <select value={draft.config.match ?? "contains"} onChange={(e) => setCfg("match", e.target.value)}>
                  <option value="contains">El mensaje contiene la palabra</option>
                  <option value="exact">El mensaje es exactamente la palabra</option>
                </select>
              </Field>
            </>
          )}
          {draft.type === "keyword_reply" && (
            <Field label="Respuesta">
              <textarea rows={3} value={draft.config.reply ?? ""} onChange={(e) => setCfg("reply", e.target.value)} />
            </Field>
          )}
          {draft.type === "keyword_handoff" && (
            <Field label="Grupo de destino (opcional)">
              <select
                value={draft.config.group_id ?? ""}
                onChange={(e) => setCfg("group_id", e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">Sin grupo</option>
                {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
            </Field>
          )}
          {draft.type === "business_hours" && (
            <>
              <Field label="Zona horaria">
                <input list="tz-list" value={draft.config.timezone ?? ""} onChange={(e) => setCfg("timezone", e.target.value)} />
                <datalist id="tz-list">{TIMEZONES.map((t) => <option key={t} value={t} />)}</datalist>
              </Field>
              <Field label="Días de atención">
                <DaysPicker value={draft.config.days ?? []} onChange={(v) => setCfg("days", v)} />
              </Field>
              <div className="grid2">
                <Field label="Desde"><input type="time" value={draft.config.start ?? ""} onChange={(e) => setCfg("start", e.target.value)} /></Field>
                <Field label="Hasta"><input type="time" value={draft.config.end ?? ""} onChange={(e) => setCfg("end", e.target.value)} /></Field>
              </div>
            </>
          )}
          {draft.type === "inactivity_close" && (
            <Field label="Horas sin mensajes">
              <input type="number" min={1} value={draft.config.hours ?? 24} onChange={(e) => setCfg("hours", Number(e.target.value))} />
            </Field>
          )}
          {draft.type !== "keyword_reply" && (
            <Field
              label={
                draft.type === "welcome" ? "Mensaje de bienvenida"
                  : draft.type === "business_hours" ? "Mensaje fuera de horario"
                  : "Mensaje al cliente (opcional)"
              }
            >
              <textarea rows={3} value={draft.config.message ?? ""} onChange={(e) => setCfg("message", e.target.value)} />
            </Field>
          )}
          <div className="grid2">
            <Field label="Prioridad" hint="Menor número = se evalúa primero.">
              <input type="number" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: Number(e.target.value) })} />
            </Field>
            <Field label="Estado">
              <Toggle checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} label={draft.enabled ? "Activa" : "Inactiva"} />
            </Field>
          </div>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
