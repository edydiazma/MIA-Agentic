"use client";

import { useEffect, useRef, useState } from "react";
import { api, fmtDateTime, fmtNum, qs, send } from "@/lib/api";
import type { CatalogSettings, Product, ProductIn, ProductList, SearchHit, SyncResult, SyncRun } from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, Field, Modal, PageHeader, Toggle, useAction, useApi } from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";

const PAGE = 50;
const EMPTY: ProductIn = {
  sku: "", name: "", description: "", category: "", brand: "", price: null, sale_price: null, currency: "COP",
  stock: null, available: true, url: "", image_url: "", attributes: {},
};
const money = (p: Product) => {
  const v = p.sale_price ?? p.price;
  return v == null ? "—" : `${p.currency} ${fmtNum(Number(v))}`;
};

function ProductModal({ initial, onClose, onSaved }: { initial: Product | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState<ProductIn>(initial ? { ...initial } : { ...EMPTY });
  const [attrs, setAttrs] = useState<[string, string][]>(Object.entries(initial?.attributes ?? {}));
  const [run, busy, error] = useAction();
  const set = <K extends keyof ProductIn>(k: K, v: ProductIn[K]) => setF((x) => ({ ...x, [k]: v }));
  const num = (v: string) => (v === "" ? null : Number(v));

  async function save() {
    const body = { ...f, attributes: Object.fromEntries(attrs.filter(([k]) => k.trim())) };
    const r = await run(() => (initial ? send(`/api/catalog/products/${initial.id}`, "PUT", body) : send("/api/catalog/products", "POST", body)));
    if (r !== undefined) onSaved();
  }

  return (
    <Modal wide title={initial ? `Producto ${initial.sku}` : "Nuevo producto"} onClose={onClose}
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy || !f.name.trim() || !f.sku.trim()} onClick={save}>Guardar</button></>}>
      <div className="grid3">
        <Field label="SKU / referencia" hint="= retailer_id en el catálogo de Meta"><input value={f.sku} disabled={!!initial} onChange={(e) => set("sku", e.target.value)} /></Field>
        <Field label="Nombre"><input value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Categoría"><input value={f.category ?? ""} onChange={(e) => set("category", e.target.value)} /></Field>
      </div>
      <Field label="Descripción"><textarea rows={3} value={f.description ?? ""} onChange={(e) => set("description", e.target.value)} /></Field>
      <div className="grid4">
        <Field label="Marca"><input value={f.brand ?? ""} onChange={(e) => set("brand", e.target.value)} /></Field>
        <Field label="Precio"><input type="number" value={f.price ?? ""} onChange={(e) => set("price", num(e.target.value))} /></Field>
        <Field label="Precio oferta"><input type="number" value={f.sale_price ?? ""} onChange={(e) => set("sale_price", num(e.target.value))} /></Field>
        <Field label="Moneda"><input maxLength={3} value={f.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} /></Field>
        <Field label="Stock"><input type="number" value={f.stock ?? ""} onChange={(e) => set("stock", num(e.target.value))} /></Field>
        <Field label="URL"><input value={f.url ?? ""} onChange={(e) => set("url", e.target.value)} /></Field>
        <Field label="Imagen (URL)"><input value={f.image_url ?? ""} onChange={(e) => set("image_url", e.target.value)} /></Field>
      </div>
      <Toggle checked={f.available} onChange={(v) => set("available", v)} label="Disponible (los agentes solo ofrecen disponibles)" />
      <div>
        <div className="row"><strong>Atributos</strong><button onClick={() => setAttrs([...attrs, ["", ""]])}>+ Atributo</button></div>
        {attrs.map(([k, v], i) => (
          <div key={i} className="inline" style={{ marginTop: 6 }}>
            <input style={{ maxWidth: 180 }} placeholder="año, color, km…" value={k}
              onChange={(e) => setAttrs(attrs.map((a, j) => (j === i ? [e.target.value, a[1]] : a)))} />
            <input placeholder="valor" value={v} onChange={(e) => setAttrs(attrs.map((a, j) => (j === i ? [a[0], e.target.value] : a)))} />
            <button className="icon" onClick={() => setAttrs(attrs.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
      </div>
      <ErrorBox error={error} />
    </Modal>
  );
}

function SettingsCard({ isAdmin, onSynced }: { isAdmin: boolean; onSynced: () => void }) {
  const s = useApi<CatalogSettings>("/api/catalog/settings");
  const [f, setF] = useState<CatalogSettings | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [run, busy, error] = useAction();
  useEffect(() => { if (s.data) setF(s.data); }, [s.data]);
  if (!f) return <Card title="Fuentes y sincronización"><ErrorBox error={s.error} /></Card>;
  const set = <K extends keyof CatalogSettings>(k: K, v: CatalogSettings[K]) => setF({ ...f, [k]: v });
  const sync = async (path: string, label: string) => {
    const r = await run(() => send<SyncResult>(path, "POST"));
    if (r) setMsg(`${label}: ${r.created} nuevos, ${r.updated} actualizados, ${r.invalid} inválidos${r.disabled ? `, ${r.disabled} desactivados` : ""}`);
    onSynced();
  };
  const last = f.last_sync as { source?: string; at?: string } | null;

  return (
    <Card title="Fuentes y sincronización">
      <div className="grid3">
        <Field label="Moneda por defecto"><input maxLength={3} disabled={!isAdmin} value={f.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} /></Field>
        <Field label="ID del catálogo de Meta" hint="Commerce Manager, conectado a tu cuenta de WhatsApp">
          <input disabled={!isAdmin} value={f.meta_catalog_id} onChange={(e) => set("meta_catalog_id", e.target.value)} />
        </Field>
        <div style={{ alignSelf: "end" }}>
          <Toggle checked={f.send_as_catalog_message} onChange={(v) => isAdmin && set("send_as_catalog_message", v)} label="Enviar como mensaje de catálogo de WhatsApp" />
        </div>
      </div>
      <div className="grid3">
        <Field label="Sistema externo: URL del feed" hint="CSV o JSON publicado por tu ERP, Shopify o inventario">
          <input disabled={!isAdmin} placeholder="https://…" value={f.feed_url} onChange={(e) => set("feed_url", e.target.value)} />
        </Field>
        <Field label="Formato">
          <select disabled={!isAdmin} value={f.feed_format} onChange={(e) => set("feed_format", e.target.value as "csv" | "json")}>
            <option value="csv">CSV</option><option value="json">JSON</option>
          </select>
        </Field>
        <Field label="Sincronizar cada (horas)" hint="0 = solo manual">
          <input type="number" min={0} disabled={!isAdmin} value={f.feed_interval_hours} onChange={(e) => set("feed_interval_hours", Number(e.target.value))} />
        </Field>
      </div>
      {isAdmin && (
        <div className="inline">
          <button className="primary" disabled={busy} onClick={async () => { const r = await run(() => send<CatalogSettings>("/api/catalog/settings", "PUT", f)); if (r) setMsg("Configuración guardada"); }}>Guardar</button>
          <button disabled={busy || !f.meta_catalog_id} onClick={() => sync("/api/catalog/sync/meta", "Meta")}>⟳ Sincronizar Meta</button>
          <button disabled={busy || !f.feed_url} onClick={() => sync("/api/catalog/sync/feed", "Feed")}>⟳ Sincronizar feed</button>
        </div>
      )}
      {last?.at && <p className="small muted">Última sincronización: {last.source} · {fmtDateTime(last.at)}</p>}
      {msg && <p className="small">{msg}</p>}
      <ErrorBox error={error} />
    </Card>
  );
}

function SearchTest() {
  const [q, setQ] = useState("");
  const [max, setMax] = useState("");
  const [res, setRes] = useState<SearchHit[] | null>(null);
  const [run, busy, error] = useAction();
  async function go() {
    const r = await run(() => api<SearchHit[]>(`/api/catalog/search${qs({ q, max_price: max })}`));
    if (r) setRes(r);
  }
  return (
    <Card title="Probar búsqueda (como la hace el agente)">
      <div className="inline">
        <input style={{ maxWidth: 320 }} placeholder="Ej.: camioneta automática 2024" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && go()} />
        <input style={{ maxWidth: 160 }} type="number" placeholder="Precio máx." value={max} onChange={(e) => setMax(e.target.value)} />
        <button disabled={busy || !q.trim()} onClick={go}>Buscar</button>
      </div>
      <ErrorBox error={error} />
      {res && (res.length === 0 ? <p className="muted">Sin resultados.</p> : (
        <ol>
          {res.map(({ product: p, as_seen_by_ai }) => (
            <li key={p.id} style={{ marginBottom: 6 }}>
              <strong>{p.name}</strong> <span className="muted small">[{p.sku}] {money(p)}</span>
              <div className="small muted">Lo que lee el agente: {as_seen_by_ai}</div>
            </li>
          ))}
        </ol>
      ))}
    </Card>
  );
}

export default function CatalogPage() {
  const isAdmin = useIsAdmin();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [available, setAvailable] = useState("true");
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  const products = useApi<ProductList>(`/api/catalog/products${qs({ q, category, available, offset, limit: PAGE })}`);
  const runs = useApi<SyncRun[]>("/api/catalog/sync-runs");
  const file = useRef<HTMLInputElement>(null);
  const [run, busy, error] = useAction();
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const items = products.data?.items ?? [];
  const total = products.data?.total ?? 0;
  const categories = products.data?.categories ?? [];

  async function importFile(f: File) {
    const fd = new FormData();
    fd.append("file", f);
    const r = await run(() => api<SyncResult>("/api/catalog/import", { method: "POST", body: fd }));
    if (r) setImportMsg(`Importación: ${r.created} nuevos, ${r.updated} actualizados, ${r.invalid} inválidos`);
    products.reload();
    runs.reload();
  }
  async function remove(p: Product) {
    if (!confirm(`¿Eliminar «${p.name}»?`)) return;
    await run(() => send(`/api/catalog/products/${p.id}`, "DELETE"));
    products.reload();
  }

  return (
    <>
      <PageHeader
        title="Catálogo de productos"
        subtitle="Los agentes con «Catálogo» activo buscan aquí (búsqueda en español) y pueden enviar productos por WhatsApp."
        actions={isAdmin ? (
          <>
            <button disabled={busy} onClick={() => file.current?.click()}>⬆ Importar CSV / Excel / JSON</button>
            <input ref={file} type="file" hidden accept=".csv,.xlsx,.xlsm,.json"
              onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) importFile(f); }} />
            <button className="primary" onClick={() => setEditing("new")}>Nuevo producto</button>
          </>
        ) : undefined}
      />
      <AdminNotice />
      <ErrorBox error={error || products.error} />
      {importMsg && <p className="small">{importMsg} · columnas reconocidas: sku/código, nombre, descripción, categoría, marca, precio, precio_oferta, stock, disponible, url, imagen; el resto se guarda como atributos.</p>}
      <Card title={`Productos (${fmtNum(total)})`} actions={
        <>
          <input style={{ width: 220 }} placeholder="Buscar" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
          <select style={{ width: "auto" }} value={category} onChange={(e) => { setCategory(e.target.value); setOffset(0); }}>
            <option value="">Todas las categorías</option>
            {categories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select style={{ width: "auto" }} value={available} onChange={(e) => { setAvailable(e.target.value); setOffset(0); }}>
            <option value="true">Disponibles</option><option value="false">No disponibles</option><option value="">Todos</option>
          </select>
        </>
      }>
        {items.length === 0 ? (
          <Empty>{products.loading ? "Cargando…" : "No hay productos. Impórtalos o sincroniza con Meta / tu sistema."}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th /><th>Producto</th><th>Categoría</th><th className="num">Precio</th><th className="num">Stock</th><th>Fuente</th><th /></tr></thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.id}>
                    <td>{p.image_url ? <img src={p.image_url} alt="" style={{ width: 44, height: 44, objectFit: "cover", borderRadius: 6 }} /> : null}</td>
                    <td><strong>{p.name}</strong><div className="small muted">{p.sku}{p.brand ? ` · ${p.brand}` : ""}</div></td>
                    <td className="small">{p.category ?? "—"}</td>
                    <td className="num">{money(p)}{p.sale_price != null && <div><Badge tone="warn">Oferta</Badge></div>}</td>
                    <td className="num">{p.stock ?? "—"}</td>
                    <td className="small">{p.source}{p.in_meta_catalog && <> · <Badge tone="info">Meta</Badge></>}{!p.available && <> · <Badge tone="neutral">No disponible</Badge></>}</td>
                    <td className="nowrap">{isAdmin && (<><button onClick={() => setEditing(p)}>Editar</button> <button className="danger" onClick={() => remove(p)}>Eliminar</button></>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="row" style={{ marginTop: 8 }}>
          <span className="small muted">{items.length ? `${offset + 1}–${offset + items.length} de ${fmtNum(total)}` : ""}</span>
          <div className="inline">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>‹</button>
            <button disabled={offset + items.length >= total} onClick={() => setOffset(offset + PAGE)}>›</button>
          </div>
        </div>
      </Card>
      <SearchTest />
      <SettingsCard isAdmin={isAdmin} onSynced={() => { products.reload(); runs.reload(); }} />
      <Card title="Historial de sincronizaciones">
        {(runs.data ?? []).length === 0 ? <Empty>Sin sincronizaciones.</Empty> : (
          <table className="table">
            <thead><tr><th>Inicio</th><th>Fuente</th><th>Estado</th><th>Resultado</th></tr></thead>
            <tbody>
              {(runs.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="small nowrap">{fmtDateTime(r.started_at)}</td>
                  <td>{r.source}</td>
                  <td><Badge tone={r.status === "done" ? "ok" : r.status === "failed" ? "bad" : "info"}>{r.status}</Badge></td>
                  <td className="small">{r.stats ? Object.entries(r.stats).map(([k, v]) => `${v} ${k}`).join(" · ") : ""}{r.error && <div className="error">{r.error}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {editing && (
        <ProductModal initial={editing === "new" ? null : editing} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); products.reload(); }} />
      )}
    </>
  );
}
