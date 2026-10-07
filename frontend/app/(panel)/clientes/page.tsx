"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  CHANNEL_ICONS,
  CHANNEL_LABELS,
  STAGE_LABEL,
  contactLabel,
  downloadUrl,
  fmtNum,
  qs,
  type AgentDetail,
  type Stage,
} from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useApi } from "@/components/ui";
import { SourceCell, type SourceFields } from "@/components/ads/SourceLine";
import { MATCHED_ON_LABEL, looksLikeKey, type GoldenSearchHit } from "@/lib/golden-types";
import ContactDetail from "@/components/clients/ContactDetail";
import { SendTemplateModal, StartConversationModal } from "@/components/clients/OutreachModals";
import { useMe } from "@/components/Shell";
import { ImportModal, NewContactModal } from "@/components/clients/ContactModals";
import {
  FALLBACK_COLUMNS,
  atomDate,
  channelLabel,
  channelText,
  fmtDays,
  relDate,
  fullDate,
  type ColumnDef,
  type ContactRow,
  type CustomerChannel,
} from "@/lib/customer-types";

const PAGE = 50;
const STORAGE_KEY = "clientes.columns.v1";
const STAGE_TONE: Record<Stage, "neutral" | "info" | "ok" | "bad"> = {
  lead: "neutral",
  prospect: "info",
  client: "ok",
  lost: "bad",
};

type Filters = {
  q: string;
  channel_ids: number[];
  agent_id: string;
  typification_id: string;
  tag: string;
  stage: string;
  created_from: string;
  created_to: string;
  updated_from: string;
  updated_to: string;
  last_interaction_from: string;
  last_interaction_to: string;
  inactive_days_gte: string;
  has_products: boolean;
  product: string;
};
const EMPTY_FILTERS: Filters = {
  q: "",
  channel_ids: [],
  agent_id: "",
  typification_id: "",
  tag: "",
  stage: "",
  created_from: "",
  created_to: "",
  updated_from: "",
  updated_to: "",
  last_interaction_from: "",
  last_interaction_to: "",
  inactive_days_gte: "",
  has_products: false,
  product: "",
};
const BASIC_FILTERS: (keyof Filters)[] = ["q", "channel_ids", "tag", "stage"];

function loadColumns(): string[] | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const v: unknown = raw ? JSON.parse(raw) : null;
    return Array.isArray(v) && v.every((k) => typeof k === "string") ? (v as string[]) : null;
  } catch {
    return null;
  }
}
function saveColumns(keys: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
  } catch {
    /* almacenamiento no disponible: la elección dura solo esta sesión */
  }
}

function filterParams(f: Filters) {
  return {
    q: f.q.trim(),
    channel_ids: f.channel_ids.join(","),
    agent_id: f.agent_id,
    typification_id: f.typification_id,
    tag: f.tag,
    stage: f.stage,
    created_from: f.created_from,
    created_to: f.created_to,
    updated_from: f.updated_from,
    updated_to: f.updated_to,
    last_interaction_from: f.last_interaction_from,
    last_interaction_to: f.last_interaction_to,
    inactive_days_gte: f.inactive_days_gte,
    has_products: f.has_products ? "true" : "",
    product: f.product.trim(),
  };
}

export default function ClientesPage() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [moreFilters, setMoreFilters] = useState(false);
  const [sort, setSort] = useState<{ key: string; order: "asc" | "desc" }>({ key: "updated_at", order: "desc" });
  const [offset, setOffset] = useState(0);
  const [modal, setModal] = useState<"new" | "import" | "columns" | null>(null);
  const [detail, setDetail] = useState<number | null>(null);
  // Selección para acciones masivas: ids marcados o «todos los del filtro»
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [allMatching, setAllMatching] = useState(false);
  const [outreach, setOutreach] = useState<"template" | "start" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const me = useMe();
  const [visible, setVisible] = useState<string[] | null>(null);

  const columnsApi = useApi<ColumnDef[]>("/api/contacts/columns");
  const allColumns: ColumnDef[] | null = columnsApi.data?.length
    ? columnsApi.data
    : columnsApi.error || (!columnsApi.loading && columnsApi.data)
      ? FALLBACK_COLUMNS
      : null;
  const tags = useApi<{ tag: string; count: number }[]>("/api/contacts/tags");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const typs = useApi<{ typifications: { id: number; name: string }[] }>("/api/conversion-actions/options");
  const companyChannels = useApi<CustomerChannel[]>("/api/channels");

  useEffect(() => {
    if (!allColumns || visible) return;
    const known = new Set(allColumns.map((c) => c.key));
    const stored = loadColumns()?.filter((k) => known.has(k));
    setVisible(stored?.length ? stored : allColumns.filter((c) => c.default_visible).map((c) => c.key));
  }, [allColumns, visible]);

  const params = filterParams(filters);
  // Búsqueda por llave del registro maestro (placa, VIN, documento, correo)
  const keyKind = looksLikeKey(filters.q);
  const golden = useApi<GoldenSearchHit[]>(keyKind ? `/api/golden/search${qs({ q: filters.q.trim() })}` : null);
  const goldenHits = keyKind && !golden.error ? golden.data ?? [] : [];
  const list = useApi<{ total: number; items: ContactRow[] }>(
    `/api/contacts${qs({ ...params, sort: sort.key, order: sort.order, offset, limit: PAGE })}`,
  );
  const total = list.data?.total ?? 0;
  const colByKey = useMemo(() => new Map((allColumns ?? []).map((c) => [c.key, c])), [allColumns]);
  const cols = (visible ?? []).map((k) => colByKey.get(k)).filter((c): c is ColumnDef => !!c);

  function set<K extends keyof Filters>(k: K, v: Filters[K]) {
    setFilters((f) => ({ ...f, [k]: v }));
    setOffset(0);
    setSelected(new Set());
    setAllMatching(false);
  }
  const pageIds = (list.data?.items ?? []).map((r) => r.id);
  const pageAllSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const selectionCount = allMatching ? total : selected.size;
  function toggle(id: number) {
    setAllMatching(false);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function togglePage() {
    setAllMatching(false);
    setSelected((prev) => {
      const next = new Set(prev);
      pageIds.forEach((id) => (pageAllSelected ? next.delete(id) : next.add(id)));
      return next;
    });
  }
  const singleSelected = !allMatching && selected.size === 1
    ? (list.data?.items ?? []).find((r) => selected.has(r.id)) ?? null
    : null;
  const selectionFilter = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== "" && v !== undefined && String(v) !== "false"),
  ) as Record<string, string | number | boolean>;
  const extraCount = (Object.keys(filters) as (keyof Filters)[]).filter((k) => {
    if (BASIC_FILTERS.includes(k)) return false;
    const v = filters[k];
    return Array.isArray(v) ? v.length > 0 : !!v;
  }).length;
  const anyFilter =
    extraCount > 0 || !!filters.q || !!filters.tag || !!filters.stage || filters.channel_ids.length > 0;

  function toggleSort(c: ColumnDef) {
    if (!c.sortable) return;
    setSort((s) =>
      s.key === c.key ? { key: c.key, order: s.order === "asc" ? "desc" : "asc" } : { key: c.key, order: "desc" },
    );
    setOffset(0);
  }

  const exportHref = downloadUrl(
    `/api/contacts/export.csv${qs({ ...params, sort: sort.key, order: sort.order, columns: cols.map((c) => c.key).join(",") })}`,
  );

  return (
    <>
      <PageHeader
        title="Clientes"
        subtitle={`${total.toLocaleString("es")} clientes`}
        actions={
          <>
            <button onClick={() => setModal("columns")} disabled={!allColumns || !visible}>
              Columnas
            </button>
            <a className="button" href={exportHref} download>
              Exportar CSV
            </a>
            <Link className="button" href="/clientes/duplicados">
              Posibles duplicados
            </Link>
            <button onClick={() => setModal("import")}>Importar CSV</button>
            <button className="primary" onClick={() => setModal("new")}>
              Nuevo cliente
            </button>
          </>
        }
      />
      {notice && (
        <div className="notice row" role="status" style={{ marginBottom: 8 }}>
          <span>{notice}</span>
          <button className="link small" onClick={() => setNotice(null)}>Cerrar</button>
        </div>
      )}
      <Card>
        <div className="inline filters" style={{ marginBottom: 8 }}>
          <input
            style={{ maxWidth: 300 }}
            placeholder="Buscar nombre, teléfono, @usuario, email, placa, VIN o documento"
            aria-label="Buscar clientes"
            value={filters.q}
            onChange={(e) => set("q", e.target.value)}
          />
          <select value={filters.tag} onChange={(e) => set("tag", e.target.value)} aria-label="Etiqueta">
            <option value="">Todas las etiquetas</option>
            {tags.data?.map((t) => (
              <option key={t.tag} value={t.tag}>
                {t.tag} ({t.count})
              </option>
            ))}
          </select>
          <select value={filters.stage} onChange={(e) => set("stage", e.target.value)} aria-label="Etapa">
            <option value="">Todas las etapas</option>
            {(Object.keys(STAGE_LABEL) as Stage[]).map((s) => (
              <option key={s} value={s}>
                {STAGE_LABEL[s]}
              </option>
            ))}
          </select>
          {(companyChannels.data?.length ?? 0) > 1 && (
            <div className="chips" role="group" aria-label="Canales de la empresa">
              {companyChannels.data!.map((ch) => {
                const on = filters.channel_ids.includes(ch.id);
                return (
                  <button
                    key={ch.id}
                    className={on ? "chip active" : "chip"}
                    aria-pressed={on}
                    title={`${CHANNEL_LABELS[ch.provider] ?? ch.provider}: ${ch.name}`}
                    onClick={() =>
                      set("channel_ids", on ? filters.channel_ids.filter((x) => x !== ch.id) : [...filters.channel_ids, ch.id])
                    }
                  >
                    {CHANNEL_ICONS[ch.provider] ?? "•"} {channelText(ch)}
                  </button>
                );
              })}
            </div>
          )}
          <button className="link small" onClick={() => setMoreFilters((v) => !v)} aria-expanded={moreFilters}>
            {moreFilters ? "Menos filtros" : `Más filtros${extraCount ? ` (${extraCount})` : ""}`}
          </button>
          {anyFilter && (
            <button
              className="link small"
              onClick={() => {
                setFilters(EMPTY_FILTERS);
                setOffset(0);
              }}
            >
              Limpiar filtros
            </button>
          )}
        </div>
        {moreFilters && (
          <div className="grid3" style={{ marginBottom: 12 }}>
            <Field label="Agente">
              <select value={filters.agent_id} onChange={(e) => set("agent_id", e.target.value)}>
                <option value="">Todos</option>
                {agents.data?.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Tipificación">
              <select value={filters.typification_id} onChange={(e) => set("typification_id", e.target.value)}>
                <option value="">Todas</option>
                {typs.data?.typifications.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Sin interacción hace al menos (días)">
              <input
                type="number"
                min={0}
                value={filters.inactive_days_gte}
                onChange={(e) => set("inactive_days_gte", e.target.value)}
              />
            </Field>
            <DateRangeField
              label="Creación"
              from={filters.created_from}
              to={filters.created_to}
              onChange={(a, b) => {
                set("created_from", a);
                set("created_to", b);
              }}
            />
            <DateRangeField
              label="Actualización"
              from={filters.updated_from}
              to={filters.updated_to}
              onChange={(a, b) => {
                set("updated_from", a);
                set("updated_to", b);
              }}
            />
            <DateRangeField
              label="Última interacción"
              from={filters.last_interaction_from}
              to={filters.last_interaction_to}
              onChange={(a, b) => {
                set("last_interaction_from", a);
                set("last_interaction_to", b);
              }}
            />
            <Field label="Producto">
              <input placeholder="Nombre o código" value={filters.product} onChange={(e) => set("product", e.target.value)} />
            </Field>
            <Field label="Productos">
              <label className="inline small">
                <input
                  type="checkbox"
                  style={{ width: "auto" }}
                  checked={filters.has_products}
                  onChange={(e) => set("has_products", e.target.checked)}
                />
                Solo clientes con productos
              </label>
            </Field>
          </div>
        )}
        <ErrorBox error={list.error} />
        {goldenHits.length > 0 && (
          <div className="card" style={{ padding: 10, marginBottom: 10, background: "var(--accent-soft)" }}>
            <div className="small strong" style={{ marginBottom: 4 }}>
              Coincidencias en datos maestros
            </div>
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {goldenHits.map((h) => (
                <li key={`${h.contact_id}-${h.matched_on}-${h.value}`}>
                  <button className="link small" onClick={() => setDetail(h.contact_id)}>
                    {h.name || `Cliente #${h.contact_id}`}
                  </button>{" "}
                  <span className="muted">
                    Encontrado por {MATCHED_ON_LABEL[h.matched_on] ?? h.matched_on} {h.value}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {(list.loading && !list.data) || !visible ? (
          <Loading />
        ) : !list.data?.items.length ? (
          <Empty>No hay clientes con esos filtros.</Empty>
        ) : (
          <div className="table-wrap">
            {selectionCount > 0 && (
              <div className="sel-bar" role="region" aria-label="Clientes seleccionados">
                <strong>{selectionCount.toLocaleString("es")} seleccionado{selectionCount === 1 ? "" : "s"}</strong>
                {!allMatching && pageAllSelected && total > pageIds.length && (
                  <button className="link small" onClick={() => setAllMatching(true)}>
                    Seleccionar los {total.toLocaleString("es")} del filtro
                  </button>
                )}
                <button className="primary" onClick={() => setOutreach("template")}>
                  Enviar plantilla
                </button>
                <button disabled={!singleSelected} title={singleSelected ? undefined : "Selecciona un solo cliente"}
                  onClick={() => setOutreach("start")}>
                  Iniciar conversación
                </button>
                <button className="link small" onClick={() => { setSelected(new Set()); setAllMatching(false); }}>
                  Quitar selección
                </button>
              </div>
            )}
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 32 }}>
                    <input type="checkbox" aria-label="Seleccionar la página" checked={pageAllSelected || allMatching}
                      onChange={togglePage} />
                  </th>
                  {cols.map((c) => {
                    const active = sort.key === c.key;
                    return (
                      <th
                        key={c.key}
                        className={c.type === "number" ? "num" : undefined}
                        aria-sort={active ? (sort.order === "asc" ? "ascending" : "descending") : undefined}
                      >
                        {c.sortable ? (
                          <button
                            className="link"
                            style={{ color: "inherit", fontWeight: 600, fontSize: "inherit" }}
                            onClick={() => toggleSort(c)}
                          >
                            {c.label}
                            {active ? (sort.order === "asc" ? " ▲" : " ▼") : ""}
                          </button>
                        ) : (
                          c.label
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((row) => (
                  <tr key={row.id} className="clickable" onClick={() => setDetail(row.id)}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" aria-label={`Seleccionar ${row.name ?? row.id}`}
                        checked={allMatching || selected.has(row.id)} onChange={() => toggle(row.id)} />
                    </td>
                    {cols.map((c) => (
                      <td
                        key={c.key}
                        className={c.type === "number" ? "num" : c.type === "date" ? "nowrap" : undefined}
                      >
                        <Cell row={row} col={c} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {total > PAGE && (
          <div className="row" style={{ marginTop: 12 }}>
            <span className="muted small">
              {offset + 1}–{Math.min(offset + PAGE, total)} de {total.toLocaleString("es")}
            </span>
            <div className="inline">
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Anterior
              </button>
              <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
                Siguiente
              </button>
            </div>
          </div>
        )}
      </Card>

      {modal === "columns" && allColumns && visible && (
        <ColumnsModal
          all={allColumns}
          visible={visible}
          onClose={() => setModal(null)}
          onSave={(keys) => {
            setVisible(keys);
            saveColumns(keys);
            setModal(null);
          }}
        />
      )}
      {modal === "new" && (
        <NewContactModal
          onClose={() => setModal(null)}
          onCreated={(c) => {
            setModal(null);
            list.reload();
            tags.reload();
            setDetail(c.id);
          }}
        />
      )}
      {modal === "import" && (
        <ImportModal
          onClose={() => setModal(null)}
          onDone={() => {
            list.reload();
            tags.reload();
          }}
        />
      )}
      {outreach === "template" && (
        <SendTemplateModal
          contactIds={allMatching ? null : [...selected]}
          filter={allMatching ? selectionFilter : null}
          count={selectionCount}
          meId={me?.id ?? null}
          onClose={() => setOutreach(null)}
          onDone={(r) => {
            setOutreach(null);
            setSelected(new Set());
            setAllMatching(false);
            setNotice(`Campaña creada: enviando a ${r.recipients.toLocaleString("es")} clientes. Revisa el avance en Campañas.`);
          }}
        />
      )}
      {outreach === "start" && singleSelected && (
        <StartConversationModal
          contactId={singleSelected.id}
          contactName={singleSelected.name ?? "el cliente"}
          onClose={() => setOutreach(null)}
        />
      )}
      {detail !== null && (
        <ContactDetail
          contactId={detail}
          onClose={() => setDetail(null)}
          onChanged={() => {
            list.reload();
            tags.reload();
          }}
        />
      )}
    </>
  );
}

function DateRangeField({
  label,
  from,
  to,
  onChange,
}: {
  label: string;
  from: string;
  to: string;
  onChange: (from: string, to: string) => void;
}) {
  return (
    <Field label={label}>
      <div className="inline" style={{ flexWrap: "nowrap" }}>
        <input type="date" value={from} aria-label={`${label} desde`} onChange={(e) => onChange(e.target.value, to)} />
        <span className="muted small">a</span>
        <input type="date" value={to} aria-label={`${label} hasta`} onChange={(e) => onChange(from, e.target.value)} />
      </div>
    </Field>
  );
}

function DateCell({ iso }: { iso: string | null | undefined }) {
  const d = relDate(iso);
  return <span title={d.title}>{d.text}</span>;
}

const Dash = () => <span className="muted">—</span>;

function Cell({ row, col }: { row: ContactRow; col: ColumnDef }) {
  const key = col.key;
  if (key === "name") {
    return (
      <>
        <span className="strong">{contactLabel(row)}</span>{" "}
        {row.marketing_opt_out && <Badge tone="warn">Opt-out</Badge>}
        {row.blocked && <Badge tone="bad">Bloqueado</Badge>}
        {row.email && <div className="muted small">{row.email}</div>}
      </>
    );
  }
  if (key === "wa_id") {
    if (row.wa_id) return <span className="nowrap">+{row.wa_id}</span>;
    if (row.wa_username) return <span className="nowrap" title={row.wa_bsuid ? `BSUID: ${row.wa_bsuid}` : undefined}>@{row.wa_username}</span>;
    return <Dash />;
  }
  if (key === "wa_username") {
    if (!row.wa_username && !row.wa_bsuid) return <Dash />;
    return (
      <span title={row.wa_bsuid ? `BSUID: ${row.wa_bsuid}` : undefined}>
        {row.wa_username ? `@${row.wa_username}` : <span className="muted small">Solo BSUID</span>}
      </span>
    );
  }
  if (key === "stage") return <Badge tone={STAGE_TONE[row.stage]}>{STAGE_LABEL[row.stage]}</Badge>;
  if (key === "tags") {
    if (!row.tags?.length) return <span className="muted">--</span>;
    return (
      <>
        {row.tags.map((t) => (
          <span key={t} className="tag">
            {t}
          </span>
        ))}
      </>
    );
  }
  if (key === "channels" || key === "channel_providers") {
    const chans = row.channels ?? [];
    if (chans.length === 1) {
      const ch = chans[0];
      return (
        <span className="nowrap" title={`${CHANNEL_LABELS[ch.provider] ?? ch.provider}: ${ch.name}`}>
          {CHANNEL_ICONS[ch.provider] ?? "•"} {channelText(ch)}
        </span>
      );
    }
    if (chans.length > 1) {
      const list = chans.map((ch) => `${CHANNEL_LABELS[ch.provider] ?? ch.provider}: ${channelText(ch)}`).join("\n");
      return (
        <span className="nowrap" title={list} aria-label={list.replaceAll("\n", ", ")} style={{ cursor: "help", textDecoration: "underline dotted" }}>
          {chans.length} Canales
        </span>
      );
    }
    const ps = row.channel_providers ?? [];
    if (!ps.length) return <Dash />;
    return <span className="nowrap">{ps.map((p) => channelLabel(p)).join(" · ")}</span>;
  }
  if (key === "last_agent") {
    return row.last_agent?.name ? <span className="nowrap">{row.last_agent.name.toUpperCase()}</span> : <Dash />;
  }
  if (key === "last_typification") {
    return row.last_typification?.name ? (
      <>{row.last_typification.name}</>
    ) : (
      <span className="muted small">NO_TIPIFICADO</span>
    );
  }
  if (key === "first_source_label" || key === "last_source_label") {
    return <SourceCell row={row as unknown as SourceFields} which={key === "first_source_label" ? "first" : "last"} />;
  }
  if (key === "created_at" || key === "updated_at") {
    const iso = row[key];
    return <span title={fullDate(iso)}>{atomDate(iso)}</span>;
  }
  if (key.startsWith("custom:")) {
    const v = row.custom_fields?.[key.slice(7)];
    return v === undefined || v === null || v === "" ? <Dash /> : <>{String(v)}</>;
  }
  const value = (row as unknown as Record<string, unknown>)[key];
  if (col.type === "date") return <DateCell iso={value as string | null | undefined} />;
  if (col.type === "ref") {
    const ref = value as { name?: string } | null | undefined;
    return ref?.name ? <>{ref.name}</> : <Dash />;
  }
  if (col.type === "number") {
    if (value === null || value === undefined) return <Dash />;
    const n = Number(value);
    return <>{key === "lifetime_days" || key === "days_since_last_interaction" ? fmtDays(n) : fmtNum(n)}</>;
  }
  if (value === null || value === undefined || value === "") return <Dash />;
  return <>{String(value)}</>;
}

function ColumnsModal({
  all,
  visible,
  onClose,
  onSave,
}: {
  all: ColumnDef[];
  visible: string[];
  onClose: () => void;
  onSave: (keys: string[]) => void;
}) {
  const [keys, setKeys] = useState<string[]>(visible);
  const groups = useMemo(() => {
    const m = new Map<string, ColumnDef[]>();
    for (const c of all) {
      const g = c.group || "Otros";
      m.set(g, [...(m.get(g) ?? []), c]);
    }
    return [...m.entries()];
  }, [all]);
  const label = (k: string) => all.find((c) => c.key === k)?.label ?? k;
  function move(i: number, d: -1 | 1) {
    const j = i + d;
    if (j < 0 || j >= keys.length) return;
    const next = [...keys];
    [next[i], next[j]] = [next[j], next[i]];
    setKeys(next);
  }
  return (
    <Modal
      wide
      title="Columnas de la lista"
      onClose={onClose}
      footer={
        <>
          <button className="link" onClick={() => setKeys(all.filter((c) => c.default_visible).map((c) => c.key))}>
            Restablecer
          </button>
          <span style={{ flex: 1 }} />
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={!keys.length} onClick={() => onSave(keys)}>
            Aplicar
          </button>
        </>
      }
    >
      <div className="grid2">
        <div className="stack">
          {groups.map(([g, cs]) => (
            <fieldset key={g} style={{ border: "none", padding: 0, margin: 0 }}>
              <legend className="strong small" style={{ marginBottom: 4 }}>
                {g}
              </legend>
              {cs.map((c) => (
                <label key={c.key} className="inline small" style={{ marginBottom: 4 }}>
                  <input
                    type="checkbox"
                    style={{ width: "auto" }}
                    checked={keys.includes(c.key)}
                    onChange={(e) =>
                      setKeys(e.target.checked ? [...keys, c.key] : keys.filter((k) => k !== c.key))
                    }
                  />
                  {c.label}
                </label>
              ))}
            </fieldset>
          ))}
        </div>
        <div>
          <div className="strong small" style={{ marginBottom: 6 }}>
            Orden ({keys.length})
          </div>
          <ol className="small" style={{ paddingLeft: 18, margin: 0 }}>
            {keys.map((k, i) => (
              <li key={k} style={{ marginBottom: 4 }}>
                <span className="inline" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
                  <span>{label(k)}</span>
                  <span className="inline" style={{ flexWrap: "nowrap", gap: 2 }}>
                    <button className="icon" aria-label={`Subir ${label(k)}`} disabled={i === 0} onClick={() => move(i, -1)}>
                      ↑
                    </button>
                    <button
                      className="icon"
                      aria-label={`Bajar ${label(k)}`}
                      disabled={i === keys.length - 1}
                      onClick={() => move(i, 1)}
                    >
                      ↓
                    </button>
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Modal>
  );
}
