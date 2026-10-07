"use client";

import { Card, Field, Loading, PageHeader } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, TIMEZONES, useIsAdmin, useSetting } from "@/components/config/common";

export default function EmpresaPage() {
  const isAdmin = useIsAdmin();
  const { draft, set, save, busy, error, saved } = useSetting("company");

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Mi empresa: datos generales y zona horaria." />
      <ConfigTabs />
      <AdminNotice />
      {!draft ? (
        <Loading />
      ) : (
        <Card title="Datos de la empresa">
          <div className="form" style={{ maxWidth: 560 }}>
            <Field label="Nombre">
              <input value={draft.name} disabled={!isAdmin} onChange={(e) => set("name", e.target.value)} />
            </Field>
            <Field label="Zona horaria" hint="Se usa en reportes, citas, horario de atención y la fecha que conoce el agente.">
              <input list="company-tz" value={draft.timezone} disabled={!isAdmin} onChange={(e) => set("timezone", e.target.value)} />
              <datalist id="company-tz">{TIMEZONES.map((t) => <option key={t} value={t} />)}</datalist>
            </Field>
            <Field label="Sitio web">
              <input value={draft.website} disabled={!isAdmin} placeholder="https://" onChange={(e) => set("website", e.target.value)} />
            </Field>
            <Field label="Dirección">
              <input value={draft.address} disabled={!isAdmin} onChange={(e) => set("address", e.target.value)} />
            </Field>
            {isAdmin && <SaveBar onSave={save} busy={busy} saved={saved} error={error} />}
          </div>
        </Card>
      )}
    </>
  );
}
