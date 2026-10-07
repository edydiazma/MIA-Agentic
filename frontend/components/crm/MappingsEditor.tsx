"use client";

import { useEffect, useState } from "react";
import { send } from "@/lib/api";
import { DIRECTION_LABEL, type CRMMapping, type CRMProvider, type MappingDirection, type MappingsResponse } from "@/lib/crm-types";
import { ErrorBox, Loading, useAction, useApi } from "@/components/ui";

const LOCAL_LABEL: Record<string, string> = {
  name: "Nombre completo",
  first_name: "Nombre",
  last_name: "Apellido",
  email: "Correo",
  phone: "Teléfono (WhatsApp)",
  stage: "Etapa del cliente",
  notes: "Notas",
  memory: "Memoria del cliente",
  "deal.name": "Nombre del negocio",
  "deal.amount": "Monto",
  "deal.stage": "Etapa del negocio",
  "deal.status": "Estado (abierto/ganado/perdido)",
  "deal.currency": "Moneda",
  "deal.close_date": "Fecha de cierre",
};
const localLabel = (k: string) => LOCAL_LABEL[k] ?? (k.startsWith("custom:") ? `Campo: ${k.slice(7)}` : k);

/** Serializa el mapa de valores como «local=remoto» por línea (fácil de editar a mano). */
const mapToText = (m: Record<string, string> | undefined) =>
  Object.entries(m ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
function textToMap(t: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const line of t.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0 && line.slice(0, i).trim()) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return Object.keys(out).length ? out : null;
}

type Row = CRMMapping & { mapText: string };
const toRow = (m: CRMMapping): Row => ({ ...m, mapText: mapToText(m.transform?.map) });

export default function MappingsEditor({ provider, readOnly }: { provider: CRMProvider; readOnly: boolean }) {
  const data = useApi<MappingsResponse>(`/api/integrations/${provider}/mappings`);
  const [rows, setRows] = useState<Row[]>([]);
  const [saved, setSaved] = useState(false);
  const [run, busy, error] = useAction();

  useEffect(() => {
    if (data.data) setRows(data.data.mappings.map(toRow));
  }, [data.data]);

  if (!data.data) return data.error ? <ErrorBox error={data.error} /> : <Loading />;
  const local = data.data.local_fields;
  const remoteHint = provider === "hubspot" ? "nombre interno, ej. firstname" : "API name, ej. FirstName";

  const patch = (i: number, p: Partial<Row>) => {
    setSaved(false);
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...p } : r)));
  };

  async function save() {
    const mappings = rows.map(({ mapText, id: _id, ...m }) => {
      const map = textToMap(mapText);
      return { ...m, transform: map ? { ...(m.transform ?? {}), map } : null };
    });
    const r = await run(() => send<MappingsResponse>(`/api/integrations/${provider}/mappings`, "PUT", { mappings }));
    if (r) {
      data.setData(r);
      setSaved(true);
    }
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <p className="small muted" style={{ margin: 0 }}>
        Qué campo de la plataforma corresponde a qué propiedad del CRM. En «Valores» puedes traducir opciones (una por
        línea, <code>valor_local=valor_crm</code>), por ejemplo etapas.
      </p>
      {(["contact", "deal"] as const).map((obj) => (
        <div key={obj} className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{obj === "contact" ? "Cliente" : "Negocio"}</th>
                <th>Propiedad en el CRM</th>
                <th>Sentido</th>
                <th>Valores</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) =>
                r.object !== obj ? null : (
                  <tr key={i}>
                    <td>
                      <select disabled={readOnly} value={r.local_field} onChange={(e) => patch(i, { local_field: e.target.value })}>
                        {local[obj].map((k) => (
                          <option key={k} value={k}>
                            {localLabel(k)}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <input
                        disabled={readOnly}
                        placeholder={remoteHint}
                        value={r.remote_property}
                        onChange={(e) => patch(i, { remote_property: e.target.value })}
                      />
                    </td>
                    <td>
                      <select
                        disabled={readOnly}
                        value={r.direction}
                        onChange={(e) => patch(i, { direction: e.target.value as MappingDirection })}
                      >
                        {(Object.keys(DIRECTION_LABEL) as MappingDirection[]).map((d) => (
                          <option key={d} value={d}>
                            {DIRECTION_LABEL[d]}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <textarea
                        disabled={readOnly}
                        rows={Math.min(4, Math.max(1, r.mapText.split("\n").length))}
                        value={r.mapText}
                        onChange={(e) => patch(i, { mapText: e.target.value })}
                        style={{ minWidth: 200, fontFamily: "monospace", fontSize: 12 }}
                      />
                    </td>
                    <td className="right">
                      {!readOnly && (
                        <button className="icon" aria-label="Quitar" onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>
                          ✕
                        </button>
                      )}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
          {!readOnly && (
            <button
              className="link small"
              onClick={() =>
                setRows((rs) => [
                  ...rs,
                  { object: obj, local_field: local[obj][0], remote_property: "", direction: "both", transform: null, mapText: "" },
                ])
              }
            >
              + Agregar campo
            </button>
          )}
        </div>
      ))}
      <ErrorBox error={error} />
      {!readOnly && (
        <div className="inline">
          <button className="primary" disabled={busy} onClick={save}>
            Guardar mapeo
          </button>
          <button
            className="link small"
            onClick={() => {
              setSaved(false);
              setRows(data.data!.defaults.map(toRow));
            }}
          >
            Restaurar valores por defecto
          </button>
          {saved && <span className="small muted">Guardado ✓</span>}
        </div>
      )}
    </div>
  );
}
