"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { fmtDate, fmtDateTime, fmtNum, send } from "@/lib/api";
import { Badge, Empty, ErrorBox, Field, Loading, Modal, useAction, useApi } from "@/components/ui";
import {
  CONSENT_LABEL,
  DOCUMENT_TYPE_LABEL,
  KEY_SOURCE_LABEL,
  RELATION_LABEL,
  SOURCE_KIND_LABEL,
  mutate,
  type Consent,
  type ContactKey,
  type GoldenResponse,
  type KeyType,
  type Vehicle,
} from "@/lib/golden-types";

/** Tipos del sistema por si /api/golden/key-types no responde (migración 23). */
const FALLBACK_TYPES: Pick<KeyType, "key" | "label" | "is_sensitive">[] = [
  { key: "phone", label: "Teléfono", is_sensitive: false },
  { key: "email", label: "Correo electrónico", is_sensitive: false },
  { key: "username", label: "Usuario de red social", is_sensitive: false },
  { key: "document", label: "Documento de identidad", is_sensitive: true },
  { key: "first_name", label: "Nombres", is_sensitive: false },
  { key: "last_name", label: "Apellidos", is_sensitive: false },
  { key: "birthdate", label: "Fecha de nacimiento", is_sensitive: true },
  { key: "address", label: "Dirección", is_sensitive: true },
  { key: "plate", label: "Placa de vehículo", is_sensitive: false },
  { key: "vin", label: "VIN / número de chasis", is_sensitive: false },
  { key: "company", label: "Empresa", is_sensitive: false },
  { key: "occupation", label: "Ocupación", is_sensitive: false },
];

const GROUPS: { title: string; keys: string[] }[] = [
  { title: "Identidad", keys: ["first_name", "last_name", "document", "birthdate", "company", "occupation"] },
  { title: "Contacto", keys: ["phone", "email", "username"] },
  { title: "Direcciones", keys: ["address"] },
  { title: "Vehículos (llaves)", keys: ["plate", "vin"] },
];
const GROUPED = new Set(GROUPS.flatMap((g) => g.keys));

const SUBTYPE_HINT: Record<string, string> = {
  document: "CC, CE, NIT, PAS, TI, RUT…",
  username: "whatsapp, instagram, facebook, tiktok…",
  address: "casa, trabajo, envío, facturación",
  phone: "móvil, fijo, trabajo",
};

/** Anillo de completitud del registro maestro. */
export function CompletenessRing({ pct, size = 56 }: { pct: number; size?: number }) {
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(100, pct || 0));
  const color = v >= 70 ? "var(--ok)" : v >= 40 ? "var(--warn)" : "var(--bad)";
  return (
    <svg width={size} height={size} role="img" aria-label={`Completitud ${v} %`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--border)" strokeWidth={6} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={6}
        strokeLinecap="round"
        strokeDasharray={`${(c * v) / 100} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="50%" dominantBaseline="central" textAnchor="middle" fontSize={size / 4.2} fontWeight={600} fill="var(--text)">
        {v}%
      </text>
    </svg>
  );
}

function SourceBadge({ k }: { k: Pick<ContactKey, "source" | "confidence" | "verified"> }) {
  const label = KEY_SOURCE_LABEL[k.source] ?? k.source;
  const isAI = k.source === "ai_conversation" || k.source === "ai_document";
  const conf = isAI && k.confidence != null ? ` ${Math.round(k.confidence * 100)}%` : "";
  return (
    <Badge tone={k.verified ? "ok" : isAI ? "info" : "neutral"}>
      {label}
      {k.verified ? " ✓" : conf}
    </Badge>
  );
}

function Evidence({ k }: { k: ContactKey }) {
  if (!k.evidence && !k.conversation_id) return null;
  return (
    <details className="small" style={{ display: "inline-block" }}>
      <summary className="link small" style={{ cursor: "pointer" }}>
        Evidencia
      </summary>
      <div className="card" style={{ padding: 8, marginTop: 4, maxWidth: 360, whiteSpace: "pre-wrap" }}>
        {k.evidence && <div>“{k.evidence}”</div>}
        <div className="muted" style={{ marginTop: 4 }}>
          Visto {fmtNum(k.seen_count)} {k.seen_count === 1 ? "vez" : "veces"} · primera {fmtDateTime(k.first_seen_at)} · última{" "}
          {fmtDateTime(k.last_seen_at)}
        </div>
        {k.conversation_id && (
          <Link href={`/conversaciones?id=${k.conversation_id}`}>Abrir conversación{k.message_id ? " (mensaje)" : ""}</Link>
        )}
      </div>
    </details>
  );
}

function KeyRow({ k, label, onChanged }: { k: ContactKey; label: string; onChanged: () => void }) {
  const [run, busy, error] = useAction();
  const act = (body: Partial<Pick<ContactKey, "rank" | "status" | "verified">>) =>
    run(async () => {
      await mutate(`/api/contact-keys/${k.id}`, "PATCH", body);
      onChanged();
    });
  const inactive = k.status !== "active";
  return (
    <li className="mk-row" style={{ opacity: inactive ? 0.6 : 1 }}>
      <div className="mk-main">
        <span className="muted small">{label}</span>
        {k.subtype && <span className="tag">{k.subtype}</span>}
        <span className={k.rank === "primary" ? "strong" : undefined} style={{ textDecoration: k.status === "rejected" ? "line-through" : undefined }}>
          {k.masked ? (
            <span title="Dato sensible: visible solo para supervisores y administradores">🔒 {k.value}</span>
          ) : (
            k.value
          )}
        </span>
        {k.rank === "primary" && !inactive && <Badge tone="info">Principal</Badge>}
        <SourceBadge k={k} />
        {k.status === "rejected" && <Badge tone="bad">Descartado</Badge>}
        {k.status === "superseded" && <Badge>Reemplazado</Badge>}
        <Evidence k={k} />
      </div>
      <div className="mk-actions">
        {!inactive && k.rank !== "primary" && (
          <button className="link small" disabled={busy} onClick={() => act({ rank: "primary" })}>
            Hacer principal
          </button>
        )}
        {!inactive && k.rank === "primary" && (
          <button className="link small" disabled={busy} onClick={() => act({ rank: "secondary" })}>
            Secundario
          </button>
        )}
        {!inactive && !k.verified && (
          <button className="link small" disabled={busy} onClick={() => act({ verified: true })}>
            Verificar
          </button>
        )}
        {!inactive ? (
          <button className="link small danger" disabled={busy} onClick={() => act({ status: "rejected" })}>
            Descartar
          </button>
        ) : (
          <button className="link small" disabled={busy} onClick={() => act({ status: "active" })}>
            Restaurar
          </button>
        )}
      </div>
      <ErrorBox error={error} />
    </li>
  );
}

function dueChip(label: string, date: string | null) {
  if (!date) return null;
  const days = Math.round((new Date(date).getTime() - Date.now()) / 86400000);
  const tone = days < 0 ? "bad" : days <= 30 ? "warn" : "neutral";
  return (
    <Badge key={label} tone={tone}>
      {label}: {fmtDate(date)}
      {days < 0 ? " (vencido)" : days <= 30 ? ` (en ${days} d)` : ""}
    </Badge>
  );
}

function VehicleCard({ v, onChanged }: { v: Vehicle; onChanged: () => void }) {
  const [run, busy, error] = useAction();
  const [editing, setEditing] = useState(false);
  return (
    <div className="card" style={{ padding: 12 }}>
      <div className="row">
        <div>
          <div className="strong">
            🚗 {[v.make, v.model, v.version].filter(Boolean).join(" ") || "Vehículo"} {v.year ? `(${v.year})` : ""}
          </div>
          <div className="small">
            {v.plate && (
              <span className="tag" style={{ fontFamily: "monospace" }}>
                {v.plate}
              </span>
            )}
            {v.vin && (
              <code className="small muted" title="VIN">
                {v.vin}
              </code>
            )}
          </div>
        </div>
        <Badge tone={v.status === "active" ? "ok" : "neutral"}>{RELATION_LABEL[v.relation] ?? v.relation}</Badge>
      </div>
      <div className="small muted" style={{ margin: "6px 0" }}>
        {[v.color, v.fuel, v.mileage_km != null ? `${fmtNum(v.mileage_km)} km` : null].filter(Boolean).join(" · ")}
        {v.confidence != null && ` · ${KEY_SOURCE_LABEL[v.source as keyof typeof KEY_SOURCE_LABEL] ?? v.source} ${Math.round(v.confidence * 100)}%`}
      </div>
      <div className="chips">
        {dueChip("SOAT", v.insurance_due)}
        {dueChip("Técnico-mecánica", v.inspection_due)}
        {dueChip("Garantía", v.warranty_until)}
        {dueChip("Mantenimiento", v.next_service_at)}
      </div>
      <div className="inline" style={{ marginTop: 8 }}>
        <button className="link small" onClick={() => setEditing(true)}>
          Editar
        </button>
        <button
          className="link small danger"
          disabled={busy}
          onClick={() =>
            window.confirm("¿Eliminar este vehículo del cliente?") &&
            run(async () => {
              await mutate(`/api/vehicles/${v.id}`, "DELETE");
              onChanged();
            })
          }
        >
          Eliminar
        </button>
      </div>
      <ErrorBox error={error} />
      {editing && (
        <VehicleModal
          initial={v}
          onClose={() => setEditing(false)}
          onSave={async (body) => {
            await mutate(`/api/vehicles/${v.id}`, "PATCH", body);
            setEditing(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

type VehicleDraft = Omit<Vehicle, "id" | "source" | "confidence" | "attributes">;
const EMPTY_VEHICLE: VehicleDraft = {
  plate: null, vin: null, make: null, model: null, version: null, year: null, color: null, fuel: null, mileage_km: null,
  relation: "owner", status: "active", insurance_due: null, inspection_due: null, warranty_until: null, next_service_at: null,
};

function VehicleModal({
  initial,
  onClose,
  onSave,
}: {
  initial?: Partial<VehicleDraft>;
  onClose: () => void;
  onSave: (body: VehicleDraft) => Promise<void>;
}) {
  const [d, setD] = useState<VehicleDraft>({ ...EMPTY_VEHICLE, ...initial });
  const [run, busy, error] = useAction();
  const set = <K extends keyof VehicleDraft>(k: K, v: VehicleDraft[K]) => setD((x) => ({ ...x, [k]: v }));
  const text = (k: keyof VehicleDraft, label: string, placeholder?: string) => (
    <Field label={label}>
      <input
        value={(d[k] as string | number | null) ?? ""}
        placeholder={placeholder}
        onChange={(e) => set(k, (e.target.value || null) as never)}
      />
    </Field>
  );
  const date = (k: keyof VehicleDraft, label: string) => (
    <Field label={label}>
      <input type="date" value={(d[k] as string | null) ?? ""} onChange={(e) => set(k, (e.target.value || null) as never)} />
    </Field>
  );
  return (
    <Modal
      title={initial && "make" in initial ? "Editar vehículo" : "Agregar vehículo"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || (!d.plate && !d.vin)} onClick={() => run(() => onSave(d))}>
            Guardar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <div className="grid2">
        {text("plate", "Placa", "ABC123")}
        {text("vin", "VIN", "17 caracteres")}
        {text("make", "Marca")}
        {text("model", "Modelo")}
        {text("version", "Versión")}
        <Field label="Año">
          <input type="number" min={1950} max={2100} value={d.year ?? ""} onChange={(e) => set("year", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        {text("color", "Color")}
        <Field label="Kilometraje">
          <input type="number" min={0} value={d.mileage_km ?? ""} onChange={(e) => set("mileage_km", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field label="Relación">
          <select value={d.relation} onChange={(e) => set("relation", e.target.value as Vehicle["relation"])}>
            {(Object.keys(RELATION_LABEL) as Vehicle["relation"][]).map((r) => (
              <option key={r} value={r}>
                {RELATION_LABEL[r]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Estado">
          <select value={d.status} onChange={(e) => set("status", e.target.value as Vehicle["status"])}>
            <option value="active">Activo</option>
            <option value="sold">Vendido</option>
            <option value="inactive">Inactivo</option>
          </select>
        </Field>
        {date("insurance_due", "Vence SOAT")}
        {date("inspection_due", "Vence técnico-mecánica")}
        {date("warranty_until", "Garantía hasta")}
        {date("next_service_at", "Próximo mantenimiento")}
      </div>
    </Modal>
  );
}

function AddKeyModal({
  contactId,
  types,
  onClose,
  onSaved,
}: {
  contactId: number;
  types: Pick<KeyType, "key" | "label">[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [d, setD] = useState({ key_type: types[0]?.key ?? "phone", subtype: "", value: "", rank: "secondary" as ContactKey["rank"] });
  const [run, busy, error] = useAction();
  async function save() {
    const ok = await run(() =>
      send(`/api/contacts/${contactId}/keys`, "POST", {
        key_type: d.key_type,
        subtype: d.subtype.trim() || null,
        value: d.value.trim(),
        rank: d.rank,
      }),
    );
    if (ok !== undefined) onSaved();
  }
  return (
    <Modal
      title="Agregar dato"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !d.value.trim()} onClick={save}>
            Agregar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <Field label="Tipo de dato">
        <select value={d.key_type} onChange={(e) => setD({ ...d, key_type: e.target.value })}>
          {types.map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
            </option>
          ))}
        </select>
      </Field>
      {SUBTYPE_HINT[d.key_type] && (
        <Field label="Subtipo" hint={SUBTYPE_HINT[d.key_type]}>
          <input value={d.subtype} onChange={(e) => setD({ ...d, subtype: e.target.value })} />
        </Field>
      )}
      <Field label="Valor" hint={d.key_type === "birthdate" ? "Formato AAAA-MM-DD" : undefined}>
        <input value={d.value} onChange={(e) => setD({ ...d, value: e.target.value })} autoFocus />
      </Field>
      <Field label="Rango">
        <select value={d.rank} onChange={(e) => setD({ ...d, rank: e.target.value as ContactKey["rank"] })}>
          <option value="secondary">Secundario</option>
          <option value="primary">Principal</option>
        </select>
      </Field>
    </Modal>
  );
}

function ConsentModal({ contactId, onClose, onSaved }: { contactId: number; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState({ consent_type: "habeas_data", granted: true, policy_version: "" });
  const [run, busy, error] = useAction();
  async function save() {
    const ok = await run(() =>
      send(`/api/contacts/${contactId}/consents`, "POST", {
        consent_type: d.consent_type,
        granted: d.granted,
        policy_version: d.policy_version.trim() || null,
      }),
    );
    if (ok !== undefined) onSaved();
  }
  return (
    <Modal
      title="Registrar consentimiento"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy} onClick={save}>
            Registrar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <Field label="Tipo">
        <select value={d.consent_type} onChange={(e) => setD({ ...d, consent_type: e.target.value })}>
          {Object.entries(CONSENT_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Respuesta del cliente">
        <select value={d.granted ? "yes" : "no"} onChange={(e) => setD({ ...d, granted: e.target.value === "yes" })}>
          <option value="yes">Acepta</option>
          <option value="no">No acepta</option>
        </select>
      </Field>
      <Field label="Versión de la política (opcional)">
        <input value={d.policy_version} onChange={(e) => setD({ ...d, policy_version: e.target.value })} placeholder="v2026-01" />
      </Field>
      <p className="muted small">Se registra con tu usuario como evidencia (Ley 1581 de 2012).</p>
    </Modal>
  );
}

function ConsentRow({ c, onChanged }: { c: Consent; onChanged: () => void }) {
  const [run, busy, error] = useAction();
  const active = !c.revoked_at;
  return (
    <li className="mk-row">
      <div className="mk-main">
        <span className="strong">{CONSENT_LABEL[c.consent_type] ?? c.consent_type}</span>
        {!active ? (
          <Badge>Revocado {fmtDate(c.revoked_at)}</Badge>
        ) : c.granted ? (
          <Badge tone="ok">Acepta</Badge>
        ) : (
          <Badge tone="bad">No acepta</Badge>
        )}
        <span className="muted small">
          {fmtDateTime(c.recorded_at)} · {KEY_SOURCE_LABEL[c.source as keyof typeof KEY_SOURCE_LABEL] ?? c.source}
          {c.policy_version ? ` · política ${c.policy_version}` : ""}
        </span>
        {c.evidence && (
          <details className="small" style={{ display: "inline-block" }}>
            <summary className="link small" style={{ cursor: "pointer" }}>
              Evidencia
            </summary>
            <div className="card" style={{ padding: 8, marginTop: 4, maxWidth: 360, whiteSpace: "pre-wrap" }}>
              “{c.evidence}”
            </div>
          </details>
        )}
      </div>
      {active && c.id != null && (
        <div className="mk-actions">
          <button
            className="link small danger"
            disabled={busy}
            onClick={() =>
              window.confirm("¿Registrar la revocación de este consentimiento?") &&
              run(async () => {
                await send(`/api/consents/${c.id}/revoke`, "POST");
                onChanged();
              })
            }
          >
            Revocar
          </button>
        </div>
      )}
      <ErrorBox error={error} />
    </li>
  );
}

/** Datos maestros (registro maestro) de un cliente: llaves con evidencia, vehículos, consentimientos y extracciones. */
export default function MasterData({ contactId }: { contactId: number }) {
  const g = useApi<GoldenResponse>(`/api/contacts/${contactId}/golden`);
  const typesApi = useApi<KeyType[]>("/api/golden/key-types");
  const [modal, setModal] = useState<"key" | "vehicle" | "consent" | null>(null);
  const [showInactive, setShowInactive] = useState(false);

  const types = useMemo(() => {
    const list = typesApi.data?.filter((t) => !t.archived_at) ?? [];
    return list.length ? [...list].sort((a, b) => a.position - b.position) : (FALLBACK_TYPES as KeyType[]);
  }, [typesApi.data]);
  const labelOf = useMemo(() => {
    const m = new Map(types.map((t) => [t.key, t.label]));
    return (k: ContactKey) => k.label || m.get(k.key_type) || k.key_type;
  }, [types]);

  if (g.loading && !g.data) return <Loading />;
  if (g.error)
    return (
      <Empty>
        {/no encontrado|404|not found/i.test(g.error)
          ? "Los datos maestros todavía no están disponibles."
          : g.error}
      </Empty>
    );
  if (!g.data) return null;

  const data = g.data;
  const keys = data.keys.filter((k) => showInactive || k.status === "active");
  const byType = (ks: string[]) =>
    keys
      .filter((k) => ks.includes(k.key_type))
      .sort((a, b) => ks.indexOf(a.key_type) - ks.indexOf(b.key_type) || (a.rank === "primary" ? -1 : 1) - (b.rank === "primary" ? -1 : 1));
  const own = keys.filter((k) => !GROUPED.has(k.key_type));
  const inactiveCount = data.keys.filter((k) => k.status !== "active").length;
  const pct = data.golden?.completeness_pct ?? data.completeness_pct ?? 0;
  const reload = () => g.reload();

  return (
    <div className="stack master-data">
      <div className="row" style={{ alignItems: "center" }}>
        <div className="inline" style={{ gap: 12 }}>
          <CompletenessRing pct={pct} />
          <div>
            <div className="strong">{data.golden?.full_name || "Registro maestro"}</div>
            <div className="muted small">
              {fmtNum(data.golden?.keys_count ?? data.keys.length)} datos · {fmtNum(data.vehicles.length)} vehículos ·{" "}
              {fmtNum(data.extractions.length)} extracciones de IA
            </div>
            <div className="muted small">Se completa solo con lo que el cliente escribe o envía (documentos, fotos, audios).</div>
          </div>
        </div>
        <div className="inline">
          <button onClick={() => setModal("key")}>+ Dato</button>
          <button onClick={() => setModal("vehicle")}>+ Vehículo</button>
          <button onClick={() => setModal("consent")}>+ Consentimiento</button>
        </div>
      </div>
      {inactiveCount > 0 && (
        <label className="inline small muted">
          <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} />
          Mostrar descartados y reemplazados ({inactiveCount})
        </label>
      )}

      <div className="grid2">
        {GROUPS.map((group) => {
          const rows = byType(group.keys);
          return (
            <section key={group.title}>
              <h3>{group.title}</h3>
              {rows.length === 0 ? (
                <p className="muted small">Sin datos todavía.</p>
              ) : (
                <ul className="mk-list">
                  {rows.map((k) => (
                    <KeyRow key={k.id} k={k} label={labelOf(k)} onChanged={reload} />
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>

      <section>
        <h3>Vehículos</h3>
        {data.vehicles.length === 0 ? (
          <p className="muted small">
            Sin vehículos. Se crean al leer la tarjeta de propiedad, el SOAT o cuando el cliente menciona su placa o VIN.
          </p>
        ) : (
          <div className="grid2">
            {data.vehicles.map((v) => (
              <VehicleCard key={v.id} v={v} onChanged={reload} />
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>Consentimientos</h3>
        {data.consents.length === 0 ? (
          <p className="muted small">Sin consentimientos registrados.</p>
        ) : (
          <ul className="mk-list">
            {data.consents.map((c, i) => (
              <ConsentRow key={c.id ?? `${c.consent_type}-${i}`} c={c} onChanged={reload} />
            ))}
          </ul>
        )}
      </section>

      {own.length > 0 && (
        <section>
          <h3>Llaves propias</h3>
          <ul className="mk-list">
            {own.map((k) => (
              <KeyRow key={k.id} k={k} label={labelOf(k)} onChanged={reload} />
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3>Historial de extracciones</h3>
        {data.extractions.length === 0 ? (
          <p className="muted small">La IA aún no ha procesado conversaciones o documentos de este cliente.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Fecha</th>
                  <th>Origen</th>
                  <th>Documento</th>
                  <th>Estado</th>
                  <th className="num">Datos nuevos</th>
                </tr>
              </thead>
              <tbody>
                {data.extractions.map((x) => (
                  <tr key={x.id}>
                    <td className="nowrap">{fmtDateTime(x.created_at)}</td>
                    <td>{SOURCE_KIND_LABEL[x.source_kind] ?? x.source_kind}</td>
                    <td>{x.document_type ? DOCUMENT_TYPE_LABEL[x.document_type] ?? x.document_type : "—"}</td>
                    <td>
                      <Badge tone={x.status === "done" ? "ok" : x.status === "failed" ? "bad" : "neutral"}>
                        {{ pending: "Pendiente", done: "Lista", failed: "Falló", skipped: "Omitida" }[x.status] ?? x.status}
                      </Badge>
                    </td>
                    <td className="num">{fmtNum(x.keys_added)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {modal === "key" && (
        <AddKeyModal
          contactId={contactId}
          types={types}
          onClose={() => setModal(null)}
          onSaved={() => {
            setModal(null);
            reload();
          }}
        />
      )}
      {modal === "vehicle" && (
        <VehicleModal
          onClose={() => setModal(null)}
          onSave={async (body) => {
            await send(`/api/contacts/${contactId}/vehicles`, "POST", body); // un error queda en el formulario
            setModal(null);
            reload();
          }}
        />
      )}
      {modal === "consent" && (
        <ConsentModal
          contactId={contactId}
          onClose={() => setModal(null)}
          onSaved={() => {
            setModal(null);
            reload();
          }}
        />
      )}
    </div>
  );
}

/** Ventana con los datos maestros (desde la bandeja). */
export function MasterDataModal({ contactId, title, onClose }: { contactId: number; title?: string; onClose: () => void }) {
  return (
    <Modal wide title={title ? `Datos maestros · ${title}` : "Datos maestros"} onClose={onClose}>
      <MasterData contactId={contactId} />
    </Modal>
  );
}
