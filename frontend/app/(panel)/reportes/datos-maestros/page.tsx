"use client";

import Link from "next/link";
import { fmtNum } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";
import { DOCUMENT_TYPE_LABEL, type GoldenReport } from "@/lib/golden-types";

export default function GoldenReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<GoldenReport>("/api/reports/golden", range);
  const c = data?.coverage;
  const pct = (n: number) => (c && c.contacts ? Math.round((100 * n) / c.contacts) : 0);

  return (
    <ReportPage
      title="Datos maestros"
      subtitle={
        <>
          Qué tan completo está el registro maestro de tus clientes y cuántos datos captura la IA de conversaciones y
          documentos. Posibles duplicados en <Link href="/clientes/duplicados">Clientes › Posibles duplicados</Link>.
        </>
      }
      range={range}
      onRange={setRange}
      loading={loading}
      error={error && /404|no encontrado/i.test(error) ? "El reporte de datos maestros todavía no está disponible." : error}
      ready={!!data}
    >
      {data && c && (
        <>
          <div className="stats">
            <Stat label="Clientes" value={fmtNum(c.contacts)} />
            <Stat
              label="Completitud promedio"
              value={c.avg_completeness == null ? "—" : `${Math.round(c.avg_completeness)} %`}
              tone={c.avg_completeness == null ? undefined : c.avg_completeness >= 60 ? "ok" : c.avg_completeness >= 30 ? "warn" : "bad"}
              hint="Nombres, apellidos, teléfono, correo, documento, nacimiento y dirección"
            />
            <Stat label="Con documento" value={`${pct(c.with_document)} %`} hint={fmtNum(c.with_document)} />
            <Stat label="Con vehículo" value={`${pct(c.with_vehicle)} %`} hint={fmtNum(c.with_vehicle)} />
            <Stat
              label="Posibles duplicados"
              value={fmtNum(data.merge_pending)}
              tone={data.merge_pending ? "warn" : undefined}
              hint={data.merge_pending ? <Link href="/clientes/duplicados">Revisar</Link> : "Sin pendientes"}
            />
          </div>

          <div className="grid2">
            <Card title="Cobertura por dato">
              {c.contacts === 0 ? (
                <Empty>Sin clientes todavía.</Empty>
              ) : (
                <BarList
                  format={(n) => `${n} %`}
                  items={[
                    { label: "Correo", value: pct(c.with_email) },
                    { label: "Documento", value: pct(c.with_document) },
                    { label: "Fecha de nacimiento", value: pct(c.with_birthdate) },
                    { label: "Dirección", value: pct(c.with_address) },
                    { label: "Vehículo (placa o VIN)", value: pct(c.with_vehicle) },
                    { label: "Teléfono secundario", value: pct(c.with_secondary_phone) },
                  ]}
                  empty="Ningún cliente tiene estos datos todavía."
                />
              )}
            </Card>
            <Card title="Documentos leídos por tipo">
              <BarList
                items={data.document_types.map((d) => ({
                  label: d.document_type ? DOCUMENT_TYPE_LABEL[d.document_type] ?? d.document_type : "Sin clasificar",
                  value: d.count,
                }))}
                empty="La IA aún no ha leído documentos en este periodo."
              />
            </Card>
          </div>

          <Card title="Extracciones de la IA por día">
            {(data.extractions_series ?? []).every((d) => d.documents + d.conversations === 0) ? (
              <Empty>Sin extracciones en este periodo.</Empty>
            ) : (
              <StackedDaily
                data={(data.extractions_series ?? []).map((d) => ({ day: d.day, conversations: d.conversations, documents: d.documents }))}
                series={[
                  { key: "conversations", label: "Conversaciones" },
                  { key: "documents", label: "Documentos" },
                ]}
                title="Extracciones por día"
              />
            )}
            <p className="muted small" style={{ marginTop: 8 }}>
              {fmtNum((data.extractions_series ?? []).reduce((a, d) => a + d.keys_added, 0))} datos nuevos capturados en el periodo.
            </p>
          </Card>
        </>
      )}
    </ReportPage>
  );
}
