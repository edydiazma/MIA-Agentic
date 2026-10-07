"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { fmtNum, fmtPct, qs } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, Stat, useApi } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { ShareBar, StackedDaily } from "@/components/reports/charts";

type Totals = {
  started: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  waiting: number;
  completion_pct: number | null;
  avg_duration_s: number | null;
};
type FlowRow = Totals & { flow_id: number; name: string; status: string; trigger_type: string };
type Summary = { totals: Totals; flows: FlowRow[]; series: ({ day: string } & Record<string, number | string>)[] };
type BlockRow = {
  block_id: string;
  block_type: string;
  label: string;
  depth: number;
  lane: string | null;
  removed: boolean;
  runs: number;
  reach_pct: number | null;
  executions: number;
  errors: number;
  exits: number;
  stuck: number;
  drop_pct: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  choices: { choice: string; runs: number; pct: number | null }[];
};
type Funnel = {
  flow: { id: number; name: string; status: string; version: number | null };
  totals: Totals;
  blocks: BlockRow[];
  top_dropoff: string | null;
};

const LANE: Record<string, string> = { then: "Si se cumple", else: "Si no", body: "Repetir", other: "Otra respuesta" };
const STATUS: Record<string, [string, "ok" | "warn" | "neutral"]> = {
  active: ["Activo", "ok"],
  paused: ["Pausado", "warn"],
  draft: ["Borrador", "neutral"],
};
const dur = (s: number | null) =>
  s == null ? "—" : s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;

export default function FlowAnalyticsPage() {
  const [range, setRange] = useDateRange();
  const [flowId, setFlowId] = useState<number | null>(null);
  useEffect(() => {
    const f = new URLSearchParams(window.location.search).get("flow");
    if (f) setFlowId(Number(f));
  }, []);
  const pick = (id: number | null) => {
    setFlowId(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("flow", String(id));
    else url.searchParams.delete("flow");
    window.history.replaceState(null, "", url);
  };

  const summary = useReport<Summary>("/api/reports/flows", range);
  const t = summary.data?.totals;

  return (
    <ReportPage
      title="Análisis de flujos"
      subtitle={
        <>
          Embudo paso a paso de cada flujo: cuántos clientes llegan a cada bloque, dónde abandonan y qué opciones
          eligen. Los flujos se editan en <Link href="/automatizaciones/flujos">Gestión de flujos</Link>.
        </>
      }
      range={range}
      onRange={setRange}
      loading={summary.loading}
      error={summary.error}
      ready={!!summary.data}
    >
      {summary.data && t && (
        <>
          <div className="stats">
            <Stat label="Ejecuciones" value={fmtNum(t.started)} />
            <Stat label="Completadas" value={fmtPct(t.completion_pct)} hint={`${fmtNum(t.succeeded)} terminaron el flujo`} />
            <Stat label="Esperando respuesta" value={fmtNum(t.waiting)} hint="Iniciadas en el periodo, aún sin terminar" />
            <Stat label="Fallidas" value={fmtNum(t.failed)} tone={t.failed ? "bad" : undefined}
              hint={`${fmtNum(t.cancelled)} canceladas`} />
            <Stat label="Duración media" value={dur(t.avg_duration_s)} hint="De las ejecuciones terminadas" />
          </div>

          <Card title="Ejecuciones por día">
            {t.started === 0 ? <Empty>Ningún flujo se ejecutó en este periodo</Empty> : (
              <StackedDaily
                data={summary.data.series}
                series={[
                  { key: "succeeded", label: "Completadas" },
                  { key: "failed", label: "Fallidas" },
                ]}
                title="Ejecuciones de flujos por día"
              />
            )}
          </Card>

          <Card title="Flujos" actions={<span className="muted small">Elige un flujo para ver su embudo</span>}>
            {summary.data.flows.length === 0 ? <Empty>No hay flujos todavía</Empty> : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Flujo</th>
                      <th className="num">Ejecuciones</th>
                      <th className="num">Completadas</th>
                      <th className="num">Esperando</th>
                      <th className="num">Fallidas</th>
                      <th className="num">Duración media</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.data.flows.map((f) => {
                      const [label, tone] = STATUS[f.status] ?? [f.status, "neutral"];
                      return (
                        <tr key={f.flow_id} className={f.flow_id === flowId ? "clickable selected" : "clickable"}
                          onClick={() => pick(f.flow_id)}>
                          <td>
                            <span className="strong">{f.name}</span> <Badge tone={tone}>{label}</Badge>
                          </td>
                          <td className="num">{fmtNum(f.started)}</td>
                          <td className="num">{fmtPct(f.completion_pct)}</td>
                          <td className="num">{fmtNum(f.waiting)}</td>
                          <td className="num">{fmtNum(f.failed)}</td>
                          <td className="num">{dur(f.avg_duration_s)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {flowId && <FunnelCard flowId={flowId} range={range} onClose={() => pick(null)} />}
        </>
      )}
    </ReportPage>
  );
}

function FunnelCard({ flowId, range, onClose }: {
  flowId: number;
  range: { start: string; end: string };
  onClose: () => void;
}) {
  const { data, error } = useApi<Funnel>(`/api/reports/flows/${flowId}${qs(range)}`);
  const title = data ? `Embudo · ${data.flow.name}${data.flow.version ? ` (v${data.flow.version})` : ""}` : "Embudo";
  const actions = (
    <span className="inline">
      <Link className="small" href={`/automatizaciones/flujos/${flowId}`}>Abrir en el editor</Link>
      <button className="link small" onClick={onClose}>Cerrar</button>
    </span>
  );
  if (error) return <Card title={title} actions={actions}><ErrorBox error={error} /></Card>;
  if (!data) return <Card title={title} actions={actions}><Loading /></Card>;
  const top = data.blocks.find((b) => b.block_id === data.top_dropoff);

  return (
    <Card title={title} actions={actions}>
      {data.totals.started === 0 ? <Empty>Este flujo no se ejecutó en el periodo elegido</Empty> : (
        <>
          {top && (
            <p className="small">
              Mayor abandono: <strong>{top.label}</strong> <span className="muted">({top.block_id})</span> —{" "}
              {fmtNum(top.exits + top.stuck)} clientes se quedaron ahí ({fmtPct(top.drop_pct)} de los que llegaron).
            </p>
          )}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Bloque</th>
                  <th style={{ minWidth: 180 }}>Llegan</th>
                  <th className="num">Abandonan</th>
                  <th className="num">Esperando</th>
                  <th className="num">Errores</th>
                  <th className="num">Latencia p50 / p95</th>
                </tr>
              </thead>
              <tbody>
                {data.blocks.map((b) => (
                  <tr key={b.block_id} className={b.block_id === data.top_dropoff ? "flagged" : undefined}>
                    <td style={{ paddingLeft: 10 + b.depth * 18 }}>
                      {b.lane && <span className="muted small">{LANE[b.lane] ?? b.lane} › </span>}
                      <span className="strong">{b.label}</span> <span className="muted small">{b.block_id}</span>
                      {b.removed && <> <Badge tone="neutral">Ya no está en el flujo</Badge></>}
                      {b.choices.length > 0 && (
                        <div style={{ marginTop: 6, maxWidth: 360 }}>
                          <ShareBar parts={b.choices.map((c) => ({
                            label: c.choice === "other" ? "Otra respuesta" : c.choice, value: c.runs }))} />
                        </div>
                      )}
                    </td>
                    <td>
                      <div className="bar-row" style={{ gridTemplateColumns: "1fr 88px" }}>
                        <div className="bar-track"><div className="bar-fill" style={{ width: `${b.reach_pct ?? 0}%` }} /></div>
                        <span className="num">{fmtNum(b.runs)} <small className="muted">{fmtPct(b.reach_pct)}</small></span>
                      </div>
                      {b.executions > b.runs && <span className="muted small">{fmtNum(b.executions)} ejecuciones (bucle)</span>}
                    </td>
                    <td className="num">{b.exits ? fmtNum(b.exits) : "—"}</td>
                    <td className="num">{b.stuck ? fmtNum(b.stuck) : "—"}</td>
                    <td className="num">{b.errors ? <Badge tone="bad">{fmtNum(b.errors)}</Badge> : "—"}</td>
                    <td className="num">
                      {b.latency_p50_ms == null ? "—" : `${fmtNum(b.latency_p50_ms)} / ${fmtNum(b.latency_p95_ms ?? 0)} ms`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            «Abandonan»: ejecuciones fallidas o canceladas cuyo último paso fue ese bloque. «Esperando»: siguen
            esperando una respuesta en ese bloque. Las barras de opciones muestran qué eligieron los clientes.
          </p>
        </>
      )}
    </Card>
  );
}
