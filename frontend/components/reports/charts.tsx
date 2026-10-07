"use client";

import { useEffect, useId, useRef, useState } from "react";
import { fmtNum } from "@/lib/api";
import { Empty } from "@/components/ui";
import s from "./viz.module.css";

/** Ranuras categóricas en orden fijo (nunca cíclico). */
export const SERIES_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
const SEQ = ["var(--seq-0)", "var(--seq-1)", "var(--seq-2)", "var(--seq-3)", "var(--seq-4)", "var(--seq-5)", "var(--seq-6)"];

export type Series = { key: string; label: string };

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(260, e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceMax(v: number): number {
  if (v <= 0) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  const n = v / pow;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * pow;
}

/** Rectángulo con las esquinas superiores redondeadas (extremo de dato), base recta. */
function topRounded(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

export function Legend({ series, line }: { series: Series[]; line?: boolean }) {
  if (series.length < 2) return null;
  return (
    <div className={s.legend}>
      {series.map((se, i) => (
        <span key={se.key} className={s.legendItem}>
          <i className={line ? s.lineKey : s.key} style={{ background: SERIES_COLORS[i] }} />
          {se.label}
        </span>
      ))}
    </div>
  );
}

const shortDay = (iso: string) => {
  const [, m, d] = iso.split("-");
  return `${d}/${m}`;
};

/**
 * Barras apiladas por día. Tooltip con todas las series al pasar el cursor (o con foco de teclado);
 * "Ver tabla" muestra los mismos datos en tabla (alivio para colores de bajo contraste).
 */
export function StackedDaily({
  data,
  series,
  title,
  height = 240,
}: {
  data: ({ day: string } & Record<string, number | string>)[];
  series: Series[];
  title: string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const titleId = useId();
  const val = (row: Record<string, number | string>, k: string) => Number(row[k] ?? 0) || 0;
  const totals = data.map((row) => series.reduce((a, se) => a + val(row, se.key), 0));
  const grand = totals.reduce((a, b) => a + b, 0);

  const pad = { l: 40, r: 8, t: 8, b: 24 };
  const max = niceMax(Math.max(0, ...totals));
  const innerW = width - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const slot = data.length ? innerW / data.length : innerW;
  const barW = Math.max(2, Math.min(36, slot * 0.62));
  const y = (v: number) => pad.t + innerH - (v / max) * innerH;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(max * f));
  const labelEvery = Math.ceil(data.length / Math.max(1, Math.floor(innerW / 48)));

  return (
    <div className={s.viz}>
      <div className={s.toolbar}>
        <Legend series={series} />
        <button className="link small" onClick={() => setTable(!table)}>
          {table ? "Ver gráfico" : "Ver tabla"}
        </button>
      </div>
      {grand === 0 ? (
        <Empty>Sin datos en este periodo</Empty>
      ) : table ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Día</th>
                {series.map((se) => (
                  <th key={se.key} className="num">
                    {se.label}
                  </th>
                ))}
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row, i) => (
                <tr key={row.day}>
                  <td>{shortDay(row.day)}</td>
                  {series.map((se) => (
                    <td key={se.key} className="num">
                      {fmtNum(val(row, se.key))}
                    </td>
                  ))}
                  <td className="num strong">{fmtNum(totals[i])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={ref} style={{ position: "relative" }} onPointerLeave={() => setHover(null)}>
          <svg className={s.svg} width={width} height={height} role="img" aria-labelledby={titleId}>
            <title id={titleId}>{title}</title>
            <desc>
              {`${data.length} días, total ${grand}. Máximo diario ${Math.max(...totals)}.`}
            </desc>
            {ticks.map((t) => (
              <g key={t}>
                <line className={s.grid} x1={pad.l} x2={width - pad.r} y1={y(t)} y2={y(t)} />
                <text className={s.axisText} x={pad.l - 6} y={y(t) + 4} textAnchor="end">
                  {fmtNum(t)}
                </text>
              </g>
            ))}
            {data.map((row, i) => {
              const cx = pad.l + slot * i + slot / 2;
              let acc = 0;
              const visible = series.map((se) => val(row, se.key)).map((v, k) => ({ v, k })).filter((p) => p.v > 0);
              return (
                <g key={row.day}>
                  {visible.map(({ v, k }, idx) => {
                    const y0 = y(acc);
                    acc += v;
                    const y1 = y(acc);
                    const h = Math.max(0, y0 - y1 - (idx > 0 ? 2 : 0)); // 2px de separación entre segmentos
                    const isTop = idx === visible.length - 1;
                    return isTop ? (
                      <path key={k} d={topRounded(cx - barW / 2, y1, barW, h, 4)} fill={SERIES_COLORS[k]} />
                    ) : (
                      <rect key={k} x={cx - barW / 2} y={y1} width={barW} height={h} fill={SERIES_COLORS[k]} />
                    );
                  })}
                  {i % labelEvery === 0 && (
                    <text className={s.axisText} x={cx} y={height - 6} textAnchor="middle">
                      {shortDay(row.day)}
                    </text>
                  )}
                  <rect
                    className={`${s.hit} ${hover === i ? s.hitActive : ""}`}
                    x={pad.l + slot * i}
                    y={pad.t}
                    width={slot}
                    height={innerH}
                    tabIndex={0}
                    aria-label={`${shortDay(row.day)}: ${series.map((se) => `${se.label} ${val(row, se.key)}`).join(", ")}`}
                    onPointerEnter={() => setHover(i)}
                    onFocus={() => setHover(i)}
                    onBlur={() => setHover(null)}
                  />
                </g>
              );
            })}
          </svg>
          {hover !== null && data[hover] && (
            <div className={s.tooltip} style={{ left: pad.l + slot * hover + slot / 2, top: y(totals[hover]) }}>
              <div className={s.tipTitle}>{shortDay(data[hover].day)}</div>
              {series.map((se, k) => (
                <div key={se.key} className={s.tipRow}>
                  <span>
                    <i className={s.lineKey} style={{ background: SERIES_COLORS[k] }} />
                    {se.label}
                  </span>
                  <strong>{fmtNum(val(data[hover], se.key))}</strong>
                </div>
              ))}
              {series.length > 1 && (
                <div className={s.tipRow}>
                  <span>Total</span>
                  <strong>{fmtNum(totals[hover])}</strong>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Lista de barras horizontales (una serie: magnitud por categoría). */
export function BarList({
  items,
  format = fmtNum,
  empty = "Sin datos en este periodo",
}: {
  items: { label: string; value: number }[];
  format?: (n: number) => string;
  empty?: string;
}) {
  const rows = items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  if (!rows.length) return <Empty>{empty}</Empty>;
  const max = Math.max(...rows.map((r) => r.value));
  const total = rows.reduce((a, r) => a + r.value, 0);
  return (
    <div className={`bars ${s.viz}`}>
      {rows.map((r) => (
        <div className="bar-row" key={r.label} title={`${r.label}: ${format(r.value)} (${Math.round((100 * r.value) / total)} %)`}>
          <span className="preview">{r.label}</span>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${(100 * r.value) / max}%`, background: "var(--series-1)" }} />
          </div>
          <span className="right strong" style={{ fontVariantNumeric: "tabular-nums" }}>
            {format(r.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Barra 100 % para participación (≤ 4 partes, con leyenda y etiquetas de porcentaje). */
export function ShareBar({ parts }: { parts: { label: string; value: number }[] }) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (!total) return <Empty>Sin datos en este periodo</Empty>;
  const pct = (v: number) => Math.round((100 * v) / total);
  return (
    <div className={s.viz}>
      <div className={s.share} role="img" aria-label={parts.map((p) => `${p.label} ${pct(p.value)} %`).join(", ")}>
        {parts.map((p, i) =>
          p.value > 0 ? (
            <div
              key={p.label}
              className={s.shareSeg}
              style={{ flexGrow: p.value, background: SERIES_COLORS[i] }}
              title={`${p.label}: ${fmtNum(p.value)} (${pct(p.value)} %)`}
            />
          ) : null,
        )}
      </div>
      <div className={s.legend} style={{ marginTop: 8 }}>
        {parts.map((p, i) => (
          <span key={p.label} className={s.legendItem}>
            <i className={s.key} style={{ background: SERIES_COLORS[i] }} />
            {p.label}: <strong style={{ color: "var(--text)" }}>{fmtNum(p.value)}</strong> ({pct(p.value)} %)
          </span>
        ))}
      </div>
    </div>
  );
}

/** Franja de 24 horas con rampa secuencial azul (más oscuro = más mensajes). */
export function HourStrip({ values }: { values: number[] }) {
  const max = Math.max(0, ...values);
  if (!max) return <Empty>Sin datos en este periodo</Empty>;
  const step = (v: number) => (v === 0 ? 0 : Math.min(6, 1 + Math.floor((v / max) * 5.999)));
  const peak = values.indexOf(max);
  return (
    <div className={s.viz}>
      <div className={s.hours} role="img" aria-label={`Mensajes por hora; la hora pico es ${peak}:00 con ${max}`}>
        {values.map((v, h) => (
          <div
            key={h}
            className={s.hourCell}
            style={{ background: SEQ[step(v)] }}
            title={`${String(h).padStart(2, "0")}:00 – ${fmtNum(v)} mensajes`}
          />
        ))}
      </div>
      <div className={s.hourLabels}>
        {values.map((_, h) => (
          <span key={h}>{h % 3 === 0 ? h : ""}</span>
        ))}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <span className="small muted">
          Hora pico: <strong style={{ color: "var(--text)" }}>{String(peak).padStart(2, "0")}:00</strong> ({fmtNum(max)}{" "}
          mensajes)
        </span>
        <span className={s.seqLegend}>
          Menos {SEQ.slice(1).map((c) => <i key={c} style={{ background: c }} />)} Más
        </span>
      </div>
    </div>
  );
}

/** Embudo ordinal (etapas en orden; tonos ordinales de la rampa azul). */
export function Funnel({ steps }: { steps: { label: string; value: number; muted?: boolean }[] }) {
  const max = Math.max(0, ...steps.map((st) => st.value));
  if (!max) return <Empty>Sin contactos todavía</Empty>;
  const tones = ["var(--seq-3)", "var(--seq-4)", "var(--seq-5)", "var(--seq-6)"];
  const first = steps[0]?.value || 0;
  return (
    <div className={`${s.viz} ${s.funnel}`}>
      {steps.map((st, i) => (
        <div key={st.label} className={s.funnelRow}>
          <span className="strong">{st.label}</span>
          <div
            className={s.funnelBar}
            style={{ width: `${(100 * st.value) / max}%`, background: st.muted ? "var(--muted)" : tones[i % tones.length] }}
            title={`${st.label}: ${fmtNum(st.value)}`}
          />
          <span className="right">
            <strong>{fmtNum(st.value)}</strong>
            {!st.muted && i > 0 && first > 0 && (
              <small className="muted"> {Math.round((100 * st.value) / first)} %</small>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

export { s as vizStyles };
