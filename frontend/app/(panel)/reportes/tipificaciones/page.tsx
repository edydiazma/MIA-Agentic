"use client";

import { fmtNum } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList } from "@/components/reports/charts";
import { pct, type GeneralReport } from "@/components/reports/types";

/** El reporte general también trae etiquetas de conversación y el acierto de la IA. */
type GeneralWithAI = GeneralReport & {
  tags?: Record<string, number>;
  ai?: { classified: number; compared: number; agreement_pct: number | null; disagreements: [string, number][] };
};

export default function TipificacionesPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<GeneralWithAI>("/api/reports/general", range);
  const rows = Object.entries(data?.typifications ?? {})
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);
  const total = rows.reduce((a, r) => a + r.value, 0);
  const untyped = data?.typifications["Sin tipificar"] ?? 0;
  const tagRows = Object.entries(data?.tags ?? {}).map(([label, value]) => ({ label, value }));
  const ai = data?.ai;

  return (
    <ReportPage
      title="Tipificaciones"
      subtitle="Motivo con el que se cerraron las conversaciones. Las opciones se editan en Configuraciones → Conversaciones."
      range={range}
      onRange={setRange}
      csv
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && (
        <>
          <div className="stats">
            <Stat label="Conversaciones cerradas" value={fmtNum(total)} />
            <Stat label="Tipificación más frecuente" value={rows[0]?.label ?? "—"} hint={rows[0] ? `${fmtNum(rows[0].value)} cierres` : undefined} />
            <Stat label="Sin tipificar" value={fmtNum(untyped)} tone={untyped ? "warn" : undefined} />
          </div>
          <Card title="Cierres por tipificación">
            {rows.length === 0 ? (
              <Empty>No se cerraron conversaciones en este periodo</Empty>
            ) : (
              <div className="grid2">
                <BarList items={rows} />
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Tipificación</th>
                        <th className="num">Cierres</th>
                        <th className="num">Participación</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={r.label}>
                          <td>{r.label}</td>
                          <td className="num">{fmtNum(r.value)}</td>
                          <td className="num">{pct(r.value, total)} %</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </Card>

          <div className="grid2" style={{ alignItems: "start", marginTop: 16 }}>
            <Card title="Etiquetas de conversación">
              <BarList items={tagRows} empty="No se etiquetaron conversaciones en este periodo" />
            </Card>

            <Card title="Acierto de la IA">
              {!ai || (ai.classified === 0 && ai.compared === 0) ? (
                <Empty>La IA no analizó conversaciones en este periodo. Actívala en Configuraciones → Clasificación IA.</Empty>
              ) : (
                <>
                  <div className="stats">
                    <Stat
                      label="Coincidencia con el asesor"
                      value={ai.agreement_pct == null ? "—" : `${ai.agreement_pct} %`}
                      hint={`${fmtNum(ai.compared)} cierres comparados`}
                      tone={ai.agreement_pct == null ? undefined : ai.agreement_pct >= 80 ? "ok" : ai.agreement_pct >= 60 ? "warn" : "bad"}
                    />
                    <Stat label="Conversaciones analizadas" value={fmtNum(ai.classified)} />
                  </div>
                  <p className="small muted" style={{ marginTop: 0 }}>
                    Compara la tipificación que propuso la IA con la que eligió el asesor al cerrar. Si coinciden poco,
                    afina los criterios de cada tipificación.
                  </p>
                  {ai.disagreements.length > 0 ? (
                    <div className="table-wrap">
                      <table className="table">
                        <thead><tr><th>IA → Asesor</th><th className="num">Veces</th></tr></thead>
                        <tbody>
                          {ai.disagreements.map(([pair, n]) => (
                            <tr key={pair}><td>{pair}</td><td className="num">{fmtNum(n)}</td></tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : ai.compared > 0 && <p className="small">Sin desacuerdos en este periodo.</p>}
                </>
              )}
            </Card>
          </div>
        </>
      )}
    </ReportPage>
  );
}
