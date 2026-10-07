"use client";

import { useState } from "react";
import Link from "next/link";
import { downloadUrl, fmtDateTime, qs } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import {
  CALL_STATUS_LABEL,
  CALL_STATUS_TONE,
  HANDLER_LABEL,
  SPEAKER_LABEL,
  fmtDuration,
  type Call,
  type CallDetail,
  type CallStatus,
} from "@/lib/voice-types";
import { Badge, Card, Empty, ErrorBox, Loading, Modal, PageHeader, useApi } from "@/components/ui";
import s from "@/components/voice/voice.module.css";

const PAGE = 50;
const EVENT_LABEL: Record<string, string> = {
  connect: "Llamada entrante",
  reject: "Rechazada",
  accept: "Contestada",
  pre_accept: "Pre-aceptada",
  transfer: "Transferencia solicitada",
  transfer_timeout: "Nadie tomó la transferencia",
  bridge: "Asesor tomó la llamada",
  voice_agent_fallback: "El agente de voz no pudo contestar",
  terminate: "Colgada",
  error: "Error",
};

function CallModal({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, loading } = useApi<CallDetail>(`/api/calls/${id}`);
  return (
    <Modal title="Detalle de la llamada" onClose={onClose} wide>
      <ErrorBox error={error} />
      {loading && !data ? (
        <Loading />
      ) : data ? (
        <div className="stack">
          <div className="row">
            <div>
              <strong>{data.contact_name || `+${data.wa_id}`}</strong>
              <div className="small muted">
                {fmtDateTime(data.started_at)} · {data.handled_by ? HANDLER_LABEL[data.handled_by] : "Sin atender"} ·{" "}
                {fmtDuration(data.duration_s)}
              </div>
            </div>
            <Badge tone={CALL_STATUS_TONE[data.status]}>{CALL_STATUS_LABEL[data.status]}</Badge>
          </div>
          {data.end_reason && <p className="small muted" style={{ margin: 0 }}>Motivo: {data.end_reason}</p>}
          {data.conversation_id && (
            <Link className="small" href={`/conversaciones?id=${data.conversation_id}`}>
              Abrir la conversación →
            </Link>
          )}
          {data.summary && (
            <Card title="📝 Resumen">
              <p style={{ margin: 0 }}>{data.summary}</p>
            </Card>
          )}
          {data.has_recording && (
            <Card title="Grabación">
              <audio controls preload="none" src={downloadUrl(`/api/calls/${data.id}/recording`)} style={{ width: "100%" }} />
            </Card>
          )}
          <Card title="Transcripción">
            {data.turns.length ? (
              <div className={s.turns}>
                {data.turns.map((t, i) => (
                  <div key={i} className={`${s.turn} ${t.speaker === "contact" ? s.contact : s.bot}`}>
                    <span className="small strong">{SPEAKER_LABEL[t.speaker] ?? t.speaker}</span>
                    <span>{t.text}</span>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>Sin transcripción (las llamadas atendidas por asesores no se transcriben).</Empty>
            )}
          </Card>
          <Card title="Eventos">
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {data.events.map((e, i) => (
                <li key={i}>
                  <span className="muted">{fmtDateTime(e.created_at)}</span> · {EVENT_LABEL[e.type] ?? e.type}
                  {typeof e.payload.reason === "string" && <span className="muted"> — {e.payload.reason}</span>}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      ) : null}
    </Modal>
  );
}

export default function HistorialLlamadasPage() {
  const [status, setStatus] = useState("");
  const [handler, setHandler] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const { data, error, loading, reload } = useApi<{ total: number; items: Call[] }>(
    `/api/calls${qs({ status, handled_by: handler, q, limit: PAGE, offset })}`,
  );

  useRealtime((event) => {
    if (event === "call.updated" || event === "call.incoming") reload();
  });

  return (
    <>
      <PageHeader
        title="Historial de llamadas"
        subtitle={
          <>
            Automatizaciones · Llamadas de WhatsApp de asesores y agentes de voz ·{" "}
            <Link href="/reportes/llamadas">Ver reporte</Link>
          </>
        }
      />
      <Card>
        <div className="inline" style={{ marginBottom: 12 }}>
          <input
            placeholder="Buscar cliente o número"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
            style={{ maxWidth: 260 }}
          />
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
            style={{ width: "auto" }}
            aria-label="Estado"
          >
            <option value="">Todos los estados</option>
            {(Object.keys(CALL_STATUS_LABEL) as CallStatus[]).map((k) => (
              <option key={k} value={k}>
                {CALL_STATUS_LABEL[k]}
              </option>
            ))}
          </select>
          <select
            value={handler}
            onChange={(e) => {
              setHandler(e.target.value);
              setOffset(0);
            }}
            style={{ width: "auto" }}
            aria-label="Atendida por"
          >
            <option value="">Atendida por cualquiera</option>
            <option value="agent">Asesor</option>
            <option value="voice_agent">Agente de voz</option>
          </select>
        </div>
        <ErrorBox error={error} />
        {loading && !data ? (
          <Loading />
        ) : !data?.items.length ? (
          <Empty>No hay llamadas con estos filtros.</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Fecha</th>
                    <th>Cliente</th>
                    <th>Estado</th>
                    <th>Atendida por</th>
                    <th className="num">Duración</th>
                    <th>Resumen</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((c) => (
                    <tr key={c.id} className="clickable" onClick={() => setOpen(c.id)}>
                      <td className="nowrap">{fmtDateTime(c.started_at)}</td>
                      <td>
                        {c.contact_name || `+${c.wa_id}`}
                        {c.contact_name && <div className="small muted">+{c.wa_id}</div>}
                      </td>
                      <td>
                        <Badge tone={CALL_STATUS_TONE[c.status]}>{CALL_STATUS_LABEL[c.status]}</Badge>
                      </td>
                      <td>{c.handled_by ? HANDLER_LABEL[c.handled_by] : "—"}</td>
                      <td className="num">{fmtDuration(c.duration_s)}</td>
                      <td className="small muted" style={{ maxWidth: 360 }}>
                        {c.summary ?? c.end_reason ?? ""}
                        {c.has_recording && " 🎙️"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <span className="small muted">
                {offset + 1}–{offset + data.items.length} de {data.total}
              </span>
              <div className="inline">
                <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                  Anterior
                </button>
                <button disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>
                  Siguiente
                </button>
              </div>
            </div>
          </>
        )}
      </Card>
      {open !== null && <CallModal id={open} onClose={() => setOpen(null)} />}
    </>
  );
}
