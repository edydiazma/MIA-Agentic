"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { fmtDateTime, fmtNum, send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Tabs, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import { ENTRY_LABELS, STATUS_LABELS, STATUS_TONE, type Journey } from "@/lib/journey-types";

export default function JourneysPage() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const [status, setStatus] = useState<"" | Journey["status"]>("");
  const { data, error, loading } = useApi<Journey[]>(`/api/journeys${status ? `?status=${status}` : ""}`);
  const [creating, setCreating] = useState(false);

  return (
    <>
      <PageHeader
        title="Journeys"
        subtitle="Recorridos de varios pasos por WhatsApp, correo y chat web: esperas, condiciones, pruebas A/B y metas"
        actions={isAdmin && <button className="primary" onClick={() => setCreating(true)}>Nuevo journey</button>}
      />
      <Tabs value={status} onChange={setStatus}
        tabs={[["", "Todos"], ["active", "Activos"], ["paused", "En pausa"], ["draft", "Borradores"], ["archived", "Archivados"]]} />
      <Card>
        <ErrorBox error={error} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>Aún no hay journeys.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Journey</th>
                  <th>Estado</th>
                  <th>Entrada</th>
                  <th className="num">Inscritos</th>
                  <th className="num">En curso</th>
                  <th className="num">Metas</th>
                  <th>Actualizado</th>
                </tr>
              </thead>
              <tbody>
                {data.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <Link href={`/journeys/${j.id}`}><strong>{j.name}</strong></Link>
                      {j.description && <div className="muted small">{j.description}</div>}
                    </td>
                    <td><Badge tone={STATUS_TONE[j.status]}>{STATUS_LABELS[j.status]}</Badge></td>
                    <td className="small">{ENTRY_LABELS[j.entry?.type] ?? "—"}</td>
                    <td className="num">{fmtNum(j.stats.enrolled ?? 0)}</td>
                    <td className="num">{fmtNum((j.stats.active ?? 0) + (j.stats.waiting ?? 0))}</td>
                    <td className="num">{fmtNum(j.stats.goal_met ?? 0)}</td>
                    <td>{fmtDateTime(j.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {creating && <NewJourneyModal onClose={() => setCreating(false)} />}
    </>
  );
}

function NewJourneyModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [run, busy, error] = useAction();

  async function create() {
    const j = await run(() =>
      send<Journey>("/api/journeys", "POST", {
        name: name.trim(),
        description: description.trim() || null,
        entry: { type: "manual" },
        settings: { reentry: "never", quiet_hours: { from: "20:00", to: "08:00" }, frequency_cap: { per_day: 1, per_week: 3 } },
        definition: {
          start: "s1",
          steps: [
            { id: "s1", type: "send_template", config: { template_name: "", language: "es", params: [] }, next: "w1" },
            { id: "w1", type: "wait", config: { days: 2 } },
          ],
        },
      }),
    );
    if (j) router.push(`/journeys/${j.id}`);
  }

  return (
    <Modal title="Nuevo journey" onClose={onClose}
      footer={<button className="primary" disabled={busy || !name.trim()} onClick={create}>Crear y abrir el editor</button>}>
      <div className="stack">
        <Field label="Nombre"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Renovación SOAT" /></Field>
        <Field label="Descripción"><input value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <p className="muted small">Empieza como borrador con horas de silencio (20:00–08:00) y un límite de 1 mensaje al día.</p>
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
