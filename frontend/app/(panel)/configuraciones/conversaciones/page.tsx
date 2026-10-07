"use client";

import { useState } from "react";
import { Card, Field, Loading, PageHeader, Toggle } from "@/components/ui";
import { AdminNotice, ConfigTabs, SaveBar, useIsAdmin, useSetting } from "@/components/config/common";

export default function ConversacionesConfigPage() {
  const isAdmin = useIsAdmin();
  const { draft, set, save, busy, error, saved } = useSetting("conversations");
  const [newTyp, setNewTyp] = useState("");

  const typs = draft?.typifications ?? [];
  const move = (i: number, d: -1 | 1) => {
    const next = typs.slice();
    const j = i + d;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    set("typifications", next);
  };
  const add = () => {
    const t = newTyp.trim();
    if (t && !typs.includes(t)) set("typifications", [...typs, t]);
    setNewTyp("");
  };

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Conversaciones: tipificaciones, asignación y nivel de servicio." />
      <ConfigTabs />
      <AdminNotice />
      {!draft ? (
        <Loading />
      ) : (
        <div className="grid2" style={{ alignItems: "start" }}>
          <Card title="Tipificaciones">
            <p className="small muted" style={{ marginTop: 0 }}>Motivo con el que el asesor cierra cada conversación; alimenta el reporte de tipificaciones.</p>
            <div className="stack" style={{ gap: 6 }}>
              {typs.map((t, i) => (
                <div key={t} className="row" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "6px 10px" }}>
                  <span>{t}</span>
                  {isAdmin && (
                    <span className="inline">
                      <button className="icon" onClick={() => move(i, -1)} disabled={i === 0} aria-label="Subir">↑</button>
                      <button className="icon" onClick={() => move(i, 1)} disabled={i === typs.length - 1} aria-label="Bajar">↓</button>
                      <button className="icon" onClick={() => set("typifications", typs.filter((x) => x !== t))} aria-label="Quitar">✕</button>
                    </span>
                  )}
                </div>
              ))}
            </div>
            {isAdmin && (
              <form className="inline" style={{ marginTop: 10, flexWrap: "nowrap" }} onSubmit={(e) => { e.preventDefault(); add(); }}>
                <input placeholder="Nueva tipificación" value={newTyp} onChange={(e) => setNewTyp(e.target.value)} />
                <button type="submit">Agregar</button>
              </form>
            )}
          </Card>

          <Card title="Reglas">
            <div className="form">
              <Toggle
                checked={draft.require_typification}
                onChange={(v) => isAdmin && set("require_typification", v)}
                label="Exigir tipificación al cerrar"
              />
              <div className="stack" style={{ gap: 4 }}>
                <Toggle checked={draft.auto_assign} onChange={(v) => isAdmin && set("auto_assign", v)} label="Asignación automática" />
                <small className="muted">
                  Cuando el bot transfiere, asigna la conversación al asesor conectado y «Disponible» con menos conversaciones
                  abiertas del grupo destino. Si no hay nadie, queda en la cola sin asignar.
                </small>
              </div>
              <Field label="Nivel de servicio (minutos)" hint="Tiempo máximo para la primera respuesta del asesor después de la transferencia.">
                <input
                  type="number"
                  min={1}
                  disabled={!isAdmin}
                  value={draft.sla_minutes}
                  onChange={(e) => set("sla_minutes", Number(e.target.value))}
                />
              </Field>
            </div>
          </Card>
        </div>
      )}
      {draft && isAdmin && (
        <div style={{ marginTop: 16 }}>
          <SaveBar onSave={save} busy={busy} saved={saved} error={error} />
        </div>
      )}
    </>
  );
}
