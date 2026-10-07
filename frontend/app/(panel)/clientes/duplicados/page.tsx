"use client";

import { useState } from "react";
import Link from "next/link";
import { fmtDateTime, fmtNum, send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import ContactDetail from "@/components/clients/ContactDetail";
import { MATCHED_ON_LABEL, type ContactSummary, type MergeCandidate } from "@/lib/golden-types";

const LABELS: [string, string][] = [
  ["name", "Nombre"],
  ["wa_id", "Teléfono"],
  ["wa_username", "Usuario WhatsApp"],
  ["email", "Correo"],
  ["document_number", "Documento"],
  ["created_at", "Cliente desde"],
  ["last_interaction_at", "Última interacción"],
  ["conversations_count", "Conversaciones"],
  ["keys_count", "Datos maestros"],
  ["completeness_pct", "Completitud"],
];

function fmtValue(key: string, v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (key.endsWith("_at")) return fmtDateTime(String(v));
  if (key === "wa_id") return `+${v}`;
  if (key === "wa_username") return `@${v}`;
  if (key === "completeness_pct") return `${v} %`;
  if (typeof v === "number") return fmtNum(v);
  return String(v);
}

function Side({
  c,
  matchedValues,
  onOpen,
}: {
  c: ContactSummary;
  matchedValues: Set<string>;
  onOpen: () => void;
}) {
  return (
    <div className="card" style={{ padding: 12 }}>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="strong">{c.name || `Cliente #${c.id}`}</span>
        <button className="link small" onClick={onOpen}>
          Ver ficha
        </button>
      </div>
      <dl>
        {LABELS.filter(([k]) => k in c).map(([k, label]) => {
          const text = fmtValue(k, c[k]);
          const hit = matchedValues.has(String(c[k] ?? "").toLowerCase());
          return (
            <div key={k} style={{ display: "contents" }}>
              <dt>{label}</dt>
              <dd>
                <span className={hit ? "match" : undefined}>{text}</span>
              </dd>
            </div>
          );
        })}
      </dl>
    </div>
  );
}

function CandidateCard({ m, onDone, onOpen }: { m: MergeCandidate; onDone: () => void; onOpen: (id: number) => void }) {
  const [keep, setKeep] = useState<number>(m.contact_a.id);
  const [run, busy, error] = useAction();
  const matched = new Set(m.matched.map((x) => x.value_normalized.toLowerCase()));
  const score = Math.round(m.score * 100);
  return (
    <Card
      title={
        <span className="inline">
          Coincidencia {score} %
          {m.matched.map((x) => (
            <Badge key={`${x.key_type}-${x.value_normalized}`} tone="warn">
              {MATCHED_ON_LABEL[x.key_type] ?? x.key_type}: {x.value_normalized}
            </Badge>
          ))}
        </span>
      }
      actions={<span className="muted small">{fmtDateTime(m.created_at)}</span>}
    >
      <div className="compare">
        <Side c={m.contact_a} matchedValues={matched} onOpen={() => onOpen(m.contact_a.id)} />
        <Side c={m.contact_b} matchedValues={matched} onOpen={() => onOpen(m.contact_b.id)} />
      </div>
      <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
        <label className="inline small">
          Conservar:
          <select value={keep} onChange={(e) => setKeep(Number(e.target.value))} aria-label="Cliente a conservar">
            <option value={m.contact_a.id}>{m.contact_a.name || `#${m.contact_a.id}`} (A)</option>
            <option value={m.contact_b.id}>{m.contact_b.name || `#${m.contact_b.id}`} (B)</option>
          </select>
        </label>
        <div className="inline">
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                await send(`/api/golden/merge-candidates/${m.id}/dismiss`, "POST");
                onDone();
              })
            }
          >
            No son la misma persona
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              window.confirm(
                "Se unirán las conversaciones, datos maestros, vehículos y negocios en el cliente conservado. Esta acción no se puede deshacer. ¿Continuar?",
              ) &&
              run(async () => {
                await send(`/api/golden/merge-candidates/${m.id}/merge`, "POST", { keep });
                onDone();
              })
            }
          >
            Unir clientes
          </button>
        </div>
      </div>
      <ErrorBox error={error} />
    </Card>
  );
}

export default function DuplicatesPage() {
  const list = useApi<MergeCandidate[]>("/api/golden/merge-candidates");
  const [detail, setDetail] = useState<number | null>(null);
  const notReady = !!list.error && /404|no encontrado|not found/i.test(list.error);
  return (
    <>
      <PageHeader
        title="Posibles duplicados"
        subtitle={
          <>
            Clientes distintos que comparten un documento, correo, teléfono, placa o VIN. El documento y el correo pesan más que
            la placa o el VIN (un vehículo cambia de dueño). Volver a <Link href="/clientes">Clientes</Link>.
          </>
        }
      />
      {!notReady && <ErrorBox error={list.error} />}
      {list.loading && !list.data ? (
        <Loading />
      ) : notReady ? (
        <Card>
          <Empty>La detección de duplicados todavía no está disponible.</Empty>
        </Card>
      ) : !list.data?.length ? (
        <Card>
          <Empty>No hay posibles duplicados pendientes.</Empty>
        </Card>
      ) : (
        <div className="stack">
          {list.data.map((m) => (
            <CandidateCard key={m.id} m={m} onDone={() => list.reload()} onOpen={setDetail} />
          ))}
        </div>
      )}
      {detail !== null && <ContactDetail contactId={detail} onClose={() => setDetail(null)} onChanged={() => list.reload()} />}
    </>
  );
}
