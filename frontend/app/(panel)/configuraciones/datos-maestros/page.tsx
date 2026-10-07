"use client";

import { useMemo, useState } from "react";
import { send } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Tabs, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import {
  NORMALIZERS,
  NORMALIZER_LABEL,
  SCOPE_LABEL,
  SECTIONS,
  flattenFields,
  type ApplyResult,
  type FieldScope,
  type GoldenField,
  type GoldenFieldsResponse,
  type KeyType,
  type Proposal,
  type ProposalField,
} from "@/lib/golden-types";

const SCOPES = Object.keys(SCOPE_LABEL) as FieldScope[];

// ---------- Campos ----------
function FieldEditModal({ f, onClose, onSaved }: { f: GoldenField; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState({
    section: f.section || "General",
    scope: f.scope || "contact",
    pipeline: f.pipeline ?? "",
    maps_to: f.maps_to ?? "",
    aliases: (f.aliases ?? []).join(", "),
    show_in_card: !!f.show_in_card,
  });
  const [run, busy, error] = useAction();
  async function save() {
    const ok = await run(() =>
      send(`/api/contact-fields/${f.id}`, "PUT", {
        key: f.key,
        label: f.label,
        type: f.type,
        description: f.description ?? null,
        section: d.section,
        scope: d.scope,
        pipeline: d.scope === "deal" ? d.pipeline.trim() || null : null,
        maps_to: d.maps_to.trim() || null,
        aliases: d.aliases.split(/[,;\n]/).map((a) => a.trim()).filter(Boolean),
        show_in_card: d.show_in_card,
      }),
    );
    if (ok !== undefined) onSaved();
  }
  return (
    <Modal
      title={`Organizar «${f.label}»`}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || (d.scope === "deal" && !d.pipeline.trim())} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Sección">
          <select value={d.section} onChange={(e) => setD({ ...d, section: e.target.value })}>
            {Array.from(new Set([...SECTIONS, d.section])).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Dónde vive el valor" hint="Las variables de flujo no se muestran en la ficha del cliente">
          <select value={d.scope} onChange={(e) => setD({ ...d, scope: e.target.value as FieldScope })}>
            {SCOPES.map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABEL[s]}
              </option>
            ))}
          </select>
        </Field>
        {d.scope === "deal" && (
          <Field label="Línea de negocio" hint="nuevos, usados, taller, repuestos, pqr…">
            <input value={d.pipeline} onChange={(e) => setD({ ...d, pipeline: e.target.value })} />
          </Field>
        )}
        <Field label="Destino en datos maestros" hint="Tipo de llave (document, plate…) o atributo (vehicle.mileage_km, consent.habeas_data)">
          <input value={d.maps_to} onChange={(e) => setD({ ...d, maps_to: e.target.value })} />
        </Field>
      </div>
      <Field label="Nombres anteriores (alias)" hint="Separados por coma: Placa, Numero_placa, C_taller_placa…">
        <textarea rows={2} value={d.aliases} onChange={(e) => setD({ ...d, aliases: e.target.value })} />
      </Field>
      <Toggle checked={d.show_in_card} onChange={(v) => setD({ ...d, show_in_card: v })} label="Mostrar en la ficha resumen" />
    </Modal>
  );
}

function FieldsTab({ isAdmin }: { isAdmin: boolean }) {
  const api = useApi<GoldenFieldsResponse>("/api/golden/fields");
  const [editing, setEditing] = useState<GoldenField | null>(null);
  const fields = useMemo(() => flattenFields(api.data).filter((f) => !f.archived_at), [api.data]);
  const bySection = useMemo(() => {
    const m = new Map<string, GoldenField[]>();
    for (const f of fields) m.set(f.section || "General", [...(m.get(f.section || "General") ?? []), f]);
    const order = (s: string) => (SECTIONS.indexOf(s) < 0 ? 99 : SECTIONS.indexOf(s));
    return Array.from(m.entries()).sort((a, b) => order(a[0]) - order(b[0]));
  }, [fields]);

  if (api.loading && !api.data) return <Loading />;
  if (api.error)
    return (
      <Card>
        <Empty>{/404|no encontrado/i.test(api.error) ? "La organización de campos todavía no está disponible." : api.error}</Empty>
      </Card>
    );
  if (!fields.length)
    return (
      <Card>
        <Empty>No hay campos de cliente. Créalos en Campos de cliente o usa el asistente de consolidación.</Empty>
      </Card>
    );

  return (
    <>
      {bySection.map(([section, list]) => (
        <Card key={section} title={`${section} (${list.length})`}>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Campo</th>
                  <th>Tipo</th>
                  <th>Vive en</th>
                  <th>Destino</th>
                  <th>Alias</th>
                  <th>Ficha</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((f) => (
                  <tr key={f.id}>
                    <td>
                      <span className="strong">{f.label}</span> <code className="muted small">{f.key}</code>
                    </td>
                    <td className="small">{f.type}</td>
                    <td className="small">
                      {SCOPE_LABEL[f.scope] ?? f.scope}
                      {f.scope === "deal" && f.pipeline ? ` · ${f.pipeline}` : ""}
                    </td>
                    <td className="small">{f.maps_to ? <code>{f.maps_to}</code> : <span className="muted">—</span>}</td>
                    <td className="small">
                      {(f.aliases ?? []).length ? (
                        f.aliases.map((a) => (
                          <span key={a} className="tag">
                            {a}
                          </span>
                        ))
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>{f.show_in_card ? <Badge tone="ok">Sí</Badge> : <span className="muted small">No</span>}</td>
                    <td className="right">
                      {isAdmin && (
                        <button className="link small" onClick={() => setEditing(f)}>
                          Organizar
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
      {editing && (
        <FieldEditModal
          f={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            api.reload();
          }}
        />
      )}
    </>
  );
}

// ---------- Tipos de llave ----------
type KeyDraft = Omit<KeyType, "id" | "position" | "organization_id" | "archived_at"> & { id?: number; position: number };

function KeyTypeModal({ initial, onClose, onSaved }: { initial: KeyDraft; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState(initial);
  const [run, busy, error] = useAction();
  const set = <K extends keyof KeyDraft>(k: K, v: KeyDraft[K]) => setD((x) => ({ ...x, [k]: v }));
  async function save() {
    const body = { ...d, ai_hint: d.ai_hint?.trim() || null };
    const ok = await run(() => (d.id ? send(`/api/golden/key-types/${d.id}`, "PUT", body) : send("/api/golden/key-types", "POST", body)));
    if (ok !== undefined) onSaved();
  }
  return (
    <Modal
      title={d.id ? `Editar «${initial.label}»` : "Nueva llave propia"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !d.label.trim() || !/^[a-z][a-z0-9_]{0,49}$/.test(d.key)} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Nombre">
          <input value={d.label} onChange={(e) => set("label", e.target.value)} placeholder="Número de póliza" />
        </Field>
        <Field label="Clave" hint="minúsculas y guiones bajos">
          <input value={d.key} disabled={!!d.id} onChange={(e) => set("key", e.target.value)} placeholder="numero_poliza" />
        </Field>
        <Field label="Formato">
          <select value={d.normalizer} onChange={(e) => set("normalizer", e.target.value)}>
            {NORMALIZERS.map((n) => (
              <option key={n} value={n}>
                {NORMALIZER_LABEL[n]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Orden">
          <input type="number" value={d.position} onChange={(e) => set("position", Number(e.target.value))} />
        </Field>
      </div>
      <div className="stack" style={{ gap: 6 }}>
        <Toggle checked={d.is_identifier} onChange={(v) => set("is_identifier", v)} label="Identifica a una sola persona (sirve para encontrar duplicados)" />
        <Toggle checked={d.is_sensitive} onChange={(v) => set("is_sensitive", v)} label="Dato sensible (se enmascara a los asesores)" />
        <Toggle checked={d.multi} onChange={(v) => set("multi", v)} label="Admite varios valores" />
        <Toggle checked={d.ai_extract} onChange={(v) => set("ai_extract", v)} label="La IA lo extrae de conversaciones y documentos" />
      </div>
      <Field label="Guía para la IA" hint="Formato o dónde suele aparecer">
        <textarea rows={2} value={d.ai_hint ?? ""} onChange={(e) => set("ai_hint", e.target.value)} />
      </Field>
    </Modal>
  );
}

function KeyTypesTab({ isAdmin }: { isAdmin: boolean }) {
  const api = useApi<KeyType[]>("/api/golden/key-types");
  const [editing, setEditing] = useState<KeyDraft | null>(null);
  if (api.loading && !api.data) return <Loading />;
  const list = [...(api.data ?? [])].sort((a, b) => a.position - b.position);
  return (
    <Card
      title="Tipos de dato del registro maestro"
      actions={
        isAdmin && (
          <button
            className="primary"
            onClick={() =>
              setEditing({ key: "", label: "", normalizer: "text", is_identifier: false, is_sensitive: false, multi: true, ai_extract: true, ai_hint: "", position: 200 })
            }
          >
            Nueva llave propia
          </button>
        )
      }
    >
      <ErrorBox error={api.error && !/404|no encontrado/i.test(api.error) ? api.error : null} />
      {!list.length ? (
        <Empty>{api.error ? "Los tipos de dato todavía no están disponibles." : "Sin tipos de dato."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Dato</th>
                <th>Formato</th>
                <th>Identifica</th>
                <th>Sensible</th>
                <th>Varios</th>
                <th>IA</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.map((t) => (
                <tr key={t.id}>
                  <td>
                    <span className="strong">{t.label}</span> <code className="muted small">{t.key}</code>{" "}
                    {t.organization_id == null ? <Badge>Sistema</Badge> : <Badge tone="info">Propia</Badge>}
                    {t.ai_hint && <div className="muted small">{t.ai_hint}</div>}
                  </td>
                  <td className="small">{NORMALIZER_LABEL[t.normalizer] ?? t.normalizer}</td>
                  <td>{t.is_identifier ? "✓" : ""}</td>
                  <td>{t.is_sensitive ? "🔒" : ""}</td>
                  <td>{t.multi ? "✓" : ""}</td>
                  <td>{t.ai_extract ? "✓" : ""}</td>
                  <td className="right">
                    {isAdmin && t.organization_id != null && (
                      <button className="link small" onClick={() => setEditing({ ...t, ai_hint: t.ai_hint ?? "" })}>
                        Editar
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <KeyTypeModal
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            api.reload();
          }}
        />
      )}
    </Card>
  );
}

// ---------- Asistente de consolidación ----------
function ConsolidateTab({ isAdmin }: { isAdmin: boolean }) {
  const [names, setNames] = useState("");
  const [useExisting, setUseExisting] = useState(true);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [result, setResult] = useState<ApplyResult | null>(null);
  const [run, busy, error] = useAction();
  const parsed = names.split(/[\n;]+/).map((n) => n.trim()).filter(Boolean);

  async function propose() {
    setResult(null);
    const p = await run(() =>
      send<Proposal>("/api/golden/fields/consolidate", "POST", { fields: parsed.length ? parsed : undefined, use_existing: useExisting }),
    );
    if (p) setProposal(p);
  }
  async function apply() {
    if (!proposal) return;
    const r = await run(() => send<ApplyResult>("/api/golden/fields/apply", "POST", { proposal }));
    if (r) {
      setResult(r);
      setProposal(null);
    }
  }
  const edit = (i: number, patch: Partial<ProposalField>) =>
    setProposal((p) => (p ? { ...p, fields: p.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)) } : p));
  const sourceCount = proposal?.fields.reduce((a, f) => a + (f.source_names?.length ?? 0), 0) ?? 0;
  const notes = proposal?.notes ? (Array.isArray(proposal.notes) ? proposal.notes : [proposal.notes]) : [];

  return (
    <>
      <Card title="Asistente de consolidación con IA">
        <p className="muted small" style={{ marginTop: 0 }}>
          Pega los nombres de campos de tu sistema anterior (Atom, bots, CRM, Excel) o usa los campos actuales. La IA propone un
          campo canónico por dato, su sección, dónde vive el valor y los nombres anteriores como alias — por ejemplo{" "}
          <code>Placa</code>, <code>Numero_placa</code>, <code>C_taller_placa</code> → un solo dato «Placa». Revisa antes de
          aplicar.
        </p>
        <Field label="Nombres de campos (uno por línea o separados por punto y coma)">
          <textarea
            rows={6}
            value={names}
            onChange={(e) => setNames(e.target.value)}
            placeholder={"Cedula\nNro. de documento\nPlaca\nNumero_placa\nC_taller_placa\nHabeas data\nSaludo inicial"}
          />
        </Field>
        <div className="row">
          <label className="inline small">
            <input type="checkbox" checked={useExisting} onChange={(e) => setUseExisting(e.target.checked)} />
            Incluir los campos de cliente actuales
          </label>
          <button className="primary" disabled={busy || !isAdmin || (!parsed.length && !useExisting)} onClick={propose}>
            {busy && !proposal ? "Analizando…" : "Proponer organización"}
          </button>
        </div>
        <ErrorBox error={error} />
        {result && (
          <div className="card" style={{ padding: 10, background: "var(--ok-soft, var(--accent-soft))" }}>
            Listo: {result.created} campos creados, {result.updated} actualizados, {result.archived} archivados y{" "}
            {result.values_migrated} valores migrados.
          </div>
        )}
      </Card>

      {proposal && (
        <Card
          title={`Propuesta: ${proposal.fields.length} campos a partir de ${sourceCount} nombres`}
          actions={
            <>
              <button onClick={() => setProposal(null)} disabled={busy}>
                Descartar
              </button>
              <button className="primary" onClick={apply} disabled={busy || !isAdmin}>
                {busy ? "Aplicando…" : "Aplicar"}
              </button>
            </>
          }
        >
          {notes.length > 0 && (
            <ul className="small muted" style={{ marginTop: 0 }}>
              {notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Nombres anteriores</th>
                  <th>Campo canónico</th>
                  <th>Sección</th>
                  <th>Vive en</th>
                  <th>Destino</th>
                  <th>Ficha</th>
                </tr>
              </thead>
              <tbody>
                {proposal.fields.map((f, i) => (
                  <tr key={`${f.key}-${i}`}>
                    <td className="small">
                      {(f.source_names?.length ? f.source_names : f.aliases).map((n) => (
                        <span key={n} className="tag">
                          {n}
                        </span>
                      ))}
                    </td>
                    <td>
                      <input value={f.label} aria-label="Nombre del campo" onChange={(e) => edit(i, { label: e.target.value })} />
                      <code className="muted small">{f.key}</code>
                    </td>
                    <td>
                      <select value={f.section} aria-label="Sección" onChange={(e) => edit(i, { section: e.target.value })}>
                        {Array.from(new Set([...SECTIONS, f.section])).map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <select value={f.scope} aria-label="Dónde vive" onChange={(e) => edit(i, { scope: e.target.value as FieldScope })}>
                        {SCOPES.map((s) => (
                          <option key={s} value={s}>
                            {SCOPE_LABEL[s]}
                          </option>
                        ))}
                      </select>
                      {f.scope === "deal" && (
                        <input
                          style={{ marginTop: 4 }}
                          value={f.pipeline ?? ""}
                          aria-label="Línea de negocio"
                          placeholder="línea"
                          onChange={(e) => edit(i, { pipeline: e.target.value || null })}
                        />
                      )}
                    </td>
                    <td className="small">{f.maps_to ? <code>{f.maps_to}</code> : <span className="muted">—</span>}</td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label="Mostrar en la ficha"
                        checked={f.show_in_card}
                        onChange={(e) => edit(i, { show_in_card: e.target.checked })}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

export default function MasterDataConfigPage() {
  const isAdmin = useIsAdmin();
  const [tab, setTab] = useState<"fields" | "keys" | "consolidate">("fields");
  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Campos y datos maestros: organiza los campos por sección, define qué identifica a un cliente y consolida campos duplicados."
      />
      <ConfigTabs />
      <AdminNotice />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          ["fields", "Campos por sección"],
          ["keys", "Tipos de dato"],
          ["consolidate", "Consolidar con IA"],
        ]}
      />
      {tab === "fields" && <FieldsTab isAdmin={isAdmin} />}
      {tab === "keys" && <KeyTypesTab isAdmin={isAdmin} />}
      {tab === "consolidate" && <ConsolidateTab isAdmin={isAdmin} />}
    </>
  );
}
