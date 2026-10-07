"use client";

import Link from "next/link";
import { Card, Field, Loading, PageHeader, Toggle } from "@/components/ui";
import { AdminNotice, ConfigTabs, DaysPicker, SaveBar, useIsAdmin, useSetting } from "@/components/config/common";

export default function CitasConfigPage() {
  const isAdmin = useIsAdmin();
  const { draft, set, save, busy, error, saved } = useSetting("appointments");

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Citas: disponibilidad para agendar desde WhatsApp." />
      <ConfigTabs />
      <AdminNotice />
      {!draft ? (
        <Loading />
      ) : (
        <Card title="Agenda">
          <div className="form" style={{ maxWidth: 640 }}>
            <div className="stack" style={{ gap: 4 }}>
              <Toggle checked={draft.enabled} onChange={(v) => isAdmin && set("enabled", v)} label="Permitir que el agente de IA agende citas" />
              <small className="muted">
                Con esto activo, el agente consulta los horarios libres y reserva cuando el cliente confirma fecha y hora. Las
                citas aparecen en <Link href="/seguimiento">Seguimiento</Link>.
              </small>
            </div>
            <Field label="Nombre de la cita" hint="Ej. Test drive, Visita a la sala, Valoración">
              <input value={draft.title} disabled={!isAdmin} onChange={(e) => set("title", e.target.value)} />
            </Field>
            <div className="grid3">
              <Field label="Duración (min)">
                <input type="number" min={5} step={5} disabled={!isAdmin} value={draft.duration_min} onChange={(e) => set("duration_min", Number(e.target.value))} />
              </Field>
              <Field label="Citas simultáneas" hint="Por franja horaria">
                <input type="number" min={1} disabled={!isAdmin} value={draft.capacity} onChange={(e) => set("capacity", Number(e.target.value))} />
              </Field>
              <Field label="Días de anticipación">
                <input type="number" min={1} disabled={!isAdmin} value={draft.max_days_ahead} onChange={(e) => set("max_days_ahead", Number(e.target.value))} />
              </Field>
            </div>
            <Field label="Días disponibles">
              <DaysPicker value={draft.days} disabled={!isAdmin} onChange={(v) => set("days", v)} />
            </Field>
            <div className="grid2">
              <Field label="Desde"><input type="time" disabled={!isAdmin} value={draft.start} onChange={(e) => set("start", e.target.value)} /></Field>
              <Field label="Hasta"><input type="time" disabled={!isAdmin} value={draft.end} onChange={(e) => set("end", e.target.value)} /></Field>
            </div>
            <p className="small muted" style={{ margin: 0 }}>Los horarios usan la zona horaria de <Link href="/configuraciones/empresa">Mi empresa</Link>.</p>
            {isAdmin && <SaveBar onSave={save} busy={busy} saved={saved} error={error} />}
          </div>
        </Card>
      )}
    </>
  );
}
