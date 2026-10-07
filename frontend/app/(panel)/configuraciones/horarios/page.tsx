"use client";

import { useEffect, useState } from "react";
import { send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, TIMEZONES, useIsAdmin } from "@/components/config/common";
import { DAY_KEYS, DAY_NAMES, type DayKey, type HoursOverview, type HoursRow, type Schedule } from "@/lib/ops-types";

type Editable = {
  timezone: string;
  schedule: Schedule;
  out_of_hours_message: string;
  assign_anyway: boolean;
  pause_bot: boolean;
  inherit_general: boolean;
};

const toEditable = (row: HoursRow | null, tz: string): Editable => ({
  timezone: row?.timezone ?? tz,
  schedule: row?.schedule ?? { mon: [{ from: "08:00", to: "18:00" }], tue: [{ from: "08:00", to: "18:00" }],
    wed: [{ from: "08:00", to: "18:00" }], thu: [{ from: "08:00", to: "18:00" }], fri: [{ from: "08:00", to: "18:00" }] },
  out_of_hours_message: row?.out_of_hours_message ?? "",
  assign_anyway: row?.assign_anyway ?? false,
  pause_bot: row?.pause_bot ?? false,
  inherit_general: row?.inherit_general ?? false,
});

function ScheduleEditor({ value, onChange, disabled }: { value: Schedule; onChange: (s: Schedule) => void; disabled?: boolean }) {
  const set = (day: DayKey, ranges: { from: string; to: string }[]) => onChange({ ...value, [day]: ranges });
  return (
    <div className="stack" style={{ gap: 6 }}>
      {DAY_KEYS.map((d) => {
        const ranges = value[d] ?? [];
        return (
          <div key={d} className="inline" style={{ alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ width: 90 }}><strong>{DAY_NAMES[d]}</strong></span>
            {!ranges.length && <span className="muted small">Cerrado</span>}
            {ranges.map((r, i) => (
              <span key={i} className="inline small">
                <input type="time" aria-label={`${DAY_NAMES[d]} desde`} value={r.from} disabled={disabled} style={{ width: "auto" }}
                  onChange={(e) => set(d, ranges.map((x, j) => (j === i ? { ...x, from: e.target.value } : x)))} />
                –
                <input type="time" aria-label={`${DAY_NAMES[d]} hasta`} value={r.to === "24:00" ? "23:59" : r.to} disabled={disabled} style={{ width: "auto" }}
                  onChange={(e) => set(d, ranges.map((x, j) => (j === i ? { ...x, to: e.target.value } : x)))} />
                {!disabled && <button className="link small" onClick={() => set(d, ranges.filter((_, j) => j !== i))}>Quitar</button>}
              </span>
            ))}
            {!disabled && (
              <button className="link small" onClick={() => set(d, [...ranges, ranges.length ? { from: "14:00", to: "18:00" } : { from: "08:00", to: "12:00" }])}>
                + Rango
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function HoursForm({ value, onChange, disabled, inheritable }: {
  value: Editable; onChange: (v: Editable) => void; disabled?: boolean; inheritable?: boolean;
}) {
  const locked = disabled || (inheritable && value.inherit_general);
  return (
    <div className="stack">
      {inheritable && (
        <Toggle checked={value.inherit_general} label="Usar el horario general (se copia cada vez que lo cambias)"
          onChange={(v) => !disabled && onChange({ ...value, inherit_general: v })} />
      )}
      <Field label="Zona horaria">
        <input list="bh-tz" value={value.timezone} disabled={locked} onChange={(e) => onChange({ ...value, timezone: e.target.value })} />
        <datalist id="bh-tz">{TIMEZONES.map((t) => <option key={t} value={t} />)}</datalist>
      </Field>
      <Field label="Horario" hint="Puedes tener varios rangos por día (por ejemplo 8–12 y 14–18).">
        <ScheduleEditor value={value.schedule} disabled={locked} onChange={(s) => onChange({ ...value, schedule: s })} />
      </Field>
      <Field label="Mensaje fuera de horario" hint="Se envía una vez por conversación en cada periodo cerrado.">
        <textarea rows={3} value={value.out_of_hours_message} disabled={locked}
          onChange={(e) => onChange({ ...value, out_of_hours_message: e.target.value })} />
      </Field>
      <Toggle checked={value.assign_anyway} label="Fuera de horario, igual asignar a un asesor"
        onChange={(v) => !locked && onChange({ ...value, assign_anyway: v })} />
      <Toggle checked={value.pause_bot} label="Fuera de horario, pausar el bot (los mensajes se guardan sin respuesta)"
        onChange={(v) => !locked && onChange({ ...value, pause_bot: v })} />
    </div>
  );
}

export default function HorariosPage() {
  const isAdmin = useIsAdmin();
  const { data, error, loading, reload } = useApi<HoursOverview>("/api/business-hours");
  const [general, setGeneral] = useState<Editable | null>(null);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [groupDraft, setGroupDraft] = useState<Editable | null>(null);
  const [holiday, setHoliday] = useState({ day: "", name: "", group_id: "" });
  const [run, busy, actionError] = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (data && !general) setGeneral(toEditable(data.general, data.default_timezone));
  }, [data, general]);

  useEffect(() => {
    if (!data || groupId == null) return;
    const g = data.groups.find((x) => x.group_id === groupId);
    setGroupDraft(toEditable(g?.hours ?? (g?.uses_general ? { ...(data.general as HoursRow), inherit_general: true } : null), data.default_timezone));
  }, [data, groupId]);

  async function saveGeneral() {
    if (!general) return;
    if (await run(() => send("/api/business-hours/general", "PUT", general))) { setSaved("Horario general guardado"); reload(); }
  }
  async function saveGroup() {
    if (!groupDraft || groupId == null) return;
    if (await run(() => send(`/api/business-hours/groups/${groupId}`, "PUT", groupDraft))) { setSaved("Horario del grupo guardado"); reload(); }
  }
  async function resetGroup() {
    if (groupId == null) return;
    if (await run(() => send(`/api/business-hours/groups/${groupId}`, "DELETE"))) { setGroupId(null); reload(); }
  }
  async function addHoliday() {
    const body = { day: holiday.day, name: holiday.name, group_id: holiday.group_id ? Number(holiday.group_id) : null };
    if (await run(() => send("/api/business-hours/holidays", "POST", body))) { setHoliday({ day: "", name: "", group_id: "" }); reload(); }
  }
  async function removeHoliday(id: number) {
    if (await run(() => send(`/api/business-hours/holidays/${id}`, "DELETE"))) reload();
  }

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Horarios de atención: general, por grupo y festivos." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={error || actionError} />
      {saved && <div className="notice small" role="status">{saved}</div>}
      {loading && !data ? <Loading /> : data && (
        <>
          <Card
            title="Horario general"
            actions={<Badge tone={data.open_now ? "ok" : "warn"}>{data.configured ? (data.open_now ? "Abierto ahora" : "Cerrado ahora") : "Sin horario: siempre abierto"}</Badge>}
          >
            {general && <HoursForm value={general} onChange={setGeneral} disabled={!isAdmin} />}
            {isAdmin && <div className="right"><button className="primary" disabled={busy} onClick={saveGeneral}>Guardar horario general</button></div>}
          </Card>

          <Card title="Horario por grupo">
            {!data.groups.length ? <Empty>No hay grupos.</Empty> : (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Grupo</th><th>Horario</th><th>Ahora</th><th /></tr></thead>
                  <tbody>
                    {data.groups.map((g) => (
                      <tr key={g.group_id}>
                        <td><strong>{g.group_name}</strong></td>
                        <td className="small">{g.uses_general ? "Usa el general" : "Propio"}</td>
                        <td>{g.open_now ? <Badge tone="ok">Abierto</Badge> : <Badge tone="warn">Cerrado</Badge>}</td>
                        <td className="right"><button className="small" onClick={() => setGroupId(g.group_id)}>Configurar</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {groupId != null && groupDraft && (
              <div className="stack" style={{ marginTop: 16 }}>
                <h3>{data.groups.find((g) => g.group_id === groupId)?.group_name}</h3>
                <HoursForm value={groupDraft} onChange={setGroupDraft} disabled={!isAdmin} inheritable />
                {isAdmin && (
                  <div className="inline right">
                    <button onClick={resetGroup} disabled={busy}>Volver al general</button>
                    <button className="primary" onClick={saveGroup} disabled={busy}>Guardar horario del grupo</button>
                  </div>
                )}
              </div>
            )}
          </Card>

          <Card title="Festivos" >
            {!data.holidays.length ? <Empty>Sin festivos.</Empty> : (
              <ul className="stack" style={{ gap: 4 }}>
                {data.holidays.map((h) => (
                  <li key={h.id} className="inline">
                    <strong>{h.day}</strong> {h.name}
                    <span className="muted small">{h.group_id ? data.groups.find((g) => g.group_id === h.group_id)?.group_name : "Toda la empresa"}</span>
                    {isAdmin && <button className="link small" onClick={() => removeHoliday(h.id)}>Quitar</button>}
                  </li>
                ))}
              </ul>
            )}
            {isAdmin && (
              <div className="inline" style={{ marginTop: 12, flexWrap: "wrap" }}>
                <input type="date" aria-label="Día" value={holiday.day} onChange={(e) => setHoliday({ ...holiday, day: e.target.value })} style={{ width: "auto" }} />
                <input aria-label="Nombre" placeholder="Nombre (ej. Navidad)" value={holiday.name} onChange={(e) => setHoliday({ ...holiday, name: e.target.value })} style={{ width: "auto" }} />
                <select aria-label="Aplica a" value={holiday.group_id} onChange={(e) => setHoliday({ ...holiday, group_id: e.target.value })} style={{ width: "auto" }}>
                  <option value="">Toda la empresa</option>
                  {data.groups.map((g) => <option key={g.group_id} value={g.group_id}>{g.group_name}</option>)}
                </select>
                <button onClick={addHoliday} disabled={busy || !holiday.day || !holiday.name.trim()}>Agregar festivo</button>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  );
}
