"use client";

import { useEffect, useState } from "react";
import { daysAgo, downloadUrl, isoDay, qs } from "@/lib/api";
import { DateRange, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";

/** Rango de fechas (por defecto últimos 7 días), sincronizado con ?start=&end= en la URL. */
export function useDateRange() {
  const [range, setRange] = useState({ start: daysAgo(6), end: isoDay() });
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const start = p.get("start");
    const end = p.get("end");
    if (start && end) setRange({ start, end });
  }, []);
  const change = (start: string, end: string) => {
    setRange({ start, end });
    const url = new URL(window.location.href);
    url.searchParams.set("start", start);
    url.searchParams.set("end", end);
    window.history.replaceState(null, "", url);
  };
  return [range, change] as const;
}

/** Carga un reporte del API con el rango de fechas. */
export function useReport<T>(path: string, range?: { start: string; end: string }) {
  return useApi<T>(range ? `${path}${qs(range)}` : path);
}

export default function ReportPage({
  title,
  subtitle,
  range,
  onRange,
  csv,
  actions,
  loading,
  error,
  ready,
  children,
}: {
  title: string;
  subtitle?: React.ReactNode;
  range?: { start: string; end: string };
  onRange?: (start: string, end: string) => void;
  /** Exporta el CSV de conversaciones del rango. */
  csv?: boolean;
  actions?: React.ReactNode;
  loading?: boolean;
  error?: string | null;
  /** true cuando ya hay datos para pintar (evita parpadeo al cambiar el rango). */
  ready: boolean;
  children: React.ReactNode;
}) {
  return (
    <>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={
          <>
            {actions}
            {range && onRange && <DateRange start={range.start} end={range.end} onChange={onRange} />}
            {csv && range && (
              <button onClick={() => (window.location.href = downloadUrl(`/api/reports/conversations.csv${qs(range)}`))}>
                ⬇ Exportar CSV
              </button>
            )}
          </>
        }
      />
      <ErrorBox error={error} />
      {ready ? children : loading ? <Loading /> : null}
    </>
  );
}
