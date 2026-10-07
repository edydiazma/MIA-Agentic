"use client";

import { useEffect, useState } from "react";
import { api, qs, send } from "@/lib/api";
import { Badge, ErrorBox, useAction, useApi } from "@/components/ui";
import {
  PRODUCT_SOURCE_LABEL,
  PRODUCT_STAGE_LABEL,
  PRODUCT_STAGE_TONE,
  fmtMoney,
  type CatalogProduct,
  type InteractionProduct,
  type ProductStage,
} from "@/lib/customer-types";

const STAGES = Object.keys(PRODUCT_STAGE_LABEL) as ProductStage[];

/** PATCH/DELETE: true al terminar, aunque la respuesta venga vacía (204). */
async function mutate(path: string, method: "PATCH" | "DELETE", body?: unknown): Promise<true> {
  try {
    await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    if (!(e instanceof SyntaxError)) throw e;
  }
  return true;
}

/**
 * Productos de la conversación (catálogo o código externo de la empresa): etapa, origen, cantidad y precio.
 * Con `contactId` y sin `conversationId` muestra el historial del cliente en modo lectura.
 */
export default function InteractionProducts({
  conversationId,
  contactId,
}: {
  conversationId?: number;
  contactId?: number;
}) {
  const path = conversationId
    ? `/api/conversations/${conversationId}/products`
    : contactId
      ? `/api/contacts/${contactId}/products`
      : null;
  const list = useApi<InteractionProduct[]>(path);
  const [run, busy, error] = useAction();
  const [adding, setAdding] = useState(false);
  const editable = !!conversationId;
  const rows = list.data ?? [];

  async function setStage(p: InteractionProduct, stage: ProductStage) {
    if (await run(() => mutate(`/api/interaction-products/${p.id}`, "PATCH", { stage }))) list.reload();
  }
  async function remove(p: InteractionProduct) {
    if (!confirm(`¿Quitar «${p.name}» de esta conversación?`)) return;
    if (await run(() => mutate(`/api/interaction-products/${p.id}`, "DELETE"))) list.reload();
  }

  if (list.error && !list.data) return null; // el backend aún no expone productos: no ocupar espacio

  return (
    <div>
      <div className="row">
        <h3>Productos</h3>
        {editable && !adding && (
          <button className="link small" onClick={() => setAdding(true)}>
            + Agregar
          </button>
        )}
      </div>
      {rows.length === 0 && !adding && <p className="muted small">Sin productos asociados.</p>}
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }} className="stack">
        {rows.map((p) => (
          <li key={p.id} className="small" style={{ display: "grid", gap: 4 }}>
            <div className="inline" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
              <span style={{ minWidth: 0 }}>
                <span className="strong">{p.product?.name ?? p.name}</span>
                {(p.product?.sku || p.external_ref) && (
                  <span className="muted"> · {p.product?.sku ?? p.external_ref}</span>
                )}
              </span>
              {editable && (
                <button className="icon" aria-label={`Quitar ${p.name}`} disabled={busy} onClick={() => remove(p)}>
                  ×
                </button>
              )}
            </div>
            <div className="inline" style={{ gap: 6 }}>
              {editable ? (
                <select
                  aria-label={`Etapa de ${p.name}`}
                  value={p.stage}
                  disabled={busy}
                  onChange={(e) => setStage(p, e.target.value as ProductStage)}
                  style={{ width: "auto", padding: "2px 6px" }}
                >
                  {STAGES.map((s) => (
                    <option key={s} value={s}>
                      {PRODUCT_STAGE_LABEL[s]}
                    </option>
                  ))}
                </select>
              ) : (
                <Badge tone={PRODUCT_STAGE_TONE[p.stage]}>{PRODUCT_STAGE_LABEL[p.stage]}</Badge>
              )}
              <Badge tone="neutral">
                {PRODUCT_SOURCE_LABEL[p.source] ?? p.source}
                {p.source === "ai" && p.confidence != null ? ` ${Math.round(p.confidence * 100)} %` : ""}
              </Badge>
              {(p.quantity != null || p.unit_price != null) && (
                <span className="muted">
                  {p.quantity != null ? `${p.quantity} × ` : ""}
                  {fmtMoney(p.unit_price ?? p.product?.price ?? null, p.currency ?? p.product?.currency)}
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
      {adding && conversationId && (
        <AddProduct
          conversationId={conversationId}
          onCancel={() => setAdding(false)}
          onAdded={() => {
            setAdding(false);
            list.reload();
          }}
        />
      )}
      <ErrorBox error={error} />
    </div>
  );
}

function AddProduct({
  conversationId,
  onCancel,
  onAdded,
}: {
  conversationId: number;
  onCancel: () => void;
  onAdded: () => void;
}) {
  const [mode, setMode] = useState<"catalog" | "external">("catalog");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<CatalogProduct[]>([]);
  const [picked, setPicked] = useState<CatalogProduct | null>(null);
  const [externalRef, setExternalRef] = useState("");
  const [name, setName] = useState("");
  const [stage, setStage] = useState<ProductStage>("interested");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [currency, setCurrency] = useState("");
  const [run, busy, error] = useAction();

  useEffect(() => {
    if (mode !== "catalog" || picked || q.trim().length < 2) {
      setResults([]);
      return;
    }
    const t = setTimeout(async () => {
      try {
        setResults(await api<CatalogProduct[]>(`/api/products/search${qs({ q: q.trim() })}`));
      } catch {
        setResults([]);
      }
    }, 250);
    return () => clearTimeout(t);
  }, [q, mode, picked]);

  const valid = mode === "catalog" ? !!picked : !!(externalRef.trim() || name.trim());

  async function save() {
    const body: Record<string, unknown> = { stage };
    if (mode === "catalog" && picked) body.product_id = picked.id;
    if (mode === "external") {
      if (externalRef.trim()) body.external_ref = externalRef.trim();
      body.name = name.trim() || externalRef.trim();
    }
    if (quantity) body.quantity = Number(quantity);
    if (price) body.unit_price = Number(price);
    if (currency.trim()) body.currency = currency.trim().toUpperCase();
    if (await run(() => send(`/api/conversations/${conversationId}/products`, "POST", body))) onAdded();
  }

  return (
    <div className="stack" style={{ gap: 6, marginTop: 8 }}>
      <div className="chips" role="radiogroup" aria-label="Origen del producto">
        <button className={mode === "catalog" ? "chip active" : "chip"} aria-pressed={mode === "catalog"} onClick={() => setMode("catalog")}>
          Catálogo
        </button>
        <button className={mode === "external" ? "chip active" : "chip"} aria-pressed={mode === "external"} onClick={() => setMode("external")}>
          Código externo
        </button>
      </div>
      {mode === "catalog" ? (
        picked ? (
          <div className="inline small">
            <span className="strong">{picked.name}</span>
            {picked.sku && <span className="muted">· {picked.sku}</span>}
            <button className="link small" onClick={() => setPicked(null)}>
              Cambiar
            </button>
          </div>
        ) : (
          <div style={{ position: "relative" }}>
            <input
              placeholder="Buscar en el catálogo (nombre o SKU)"
              aria-label="Buscar producto en el catálogo"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            {results.length > 0 && (
              <ul
                role="listbox"
                className="small"
                style={{
                  listStyle: "none",
                  margin: "4px 0 0",
                  padding: 4,
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  background: "var(--panel)",
                  maxHeight: 200,
                  overflowY: "auto",
                }}
              >
                {results.map((r) => (
                  <li key={r.id}>
                    <button
                      role="option"
                      aria-selected={false}
                      style={{ width: "100%", textAlign: "left", border: "none", background: "transparent" }}
                      onClick={() => {
                        setPicked(r);
                        if (r.price != null && !price) setPrice(String(r.price));
                        if (r.currency && !currency) setCurrency(r.currency);
                      }}
                    >
                      {r.name} <span className="muted">{r.sku ? `· ${r.sku}` : ""} {fmtMoney(r.price, r.currency)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      ) : (
        <>
          <input
            placeholder="Código en tu sistema (SKU, ERP, CRM)"
            aria-label="Código externo"
            value={externalRef}
            onChange={(e) => setExternalRef(e.target.value)}
          />
          <input placeholder="Nombre del producto" aria-label="Nombre del producto" value={name} onChange={(e) => setName(e.target.value)} />
        </>
      )}
      <div className="inline" style={{ flexWrap: "nowrap", gap: 6 }}>
        <select aria-label="Etapa" value={stage} onChange={(e) => setStage(e.target.value as ProductStage)}>
          {STAGES.map((s) => (
            <option key={s} value={s}>
              {PRODUCT_STAGE_LABEL[s]}
            </option>
          ))}
        </select>
        <input type="number" min={0} step="any" placeholder="Cant." aria-label="Cantidad" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
      </div>
      <div className="inline" style={{ flexWrap: "nowrap", gap: 6 }}>
        <input type="number" min={0} step="any" placeholder="Precio unitario" aria-label="Precio unitario" value={price} onChange={(e) => setPrice(e.target.value)} />
        <input placeholder="COP" aria-label="Moneda" maxLength={3} style={{ maxWidth: 70 }} value={currency} onChange={(e) => setCurrency(e.target.value)} />
      </div>
      <div className="inline">
        <button className="primary" disabled={busy || !valid} onClick={save}>
          Guardar
        </button>
        <button onClick={onCancel}>Cancelar</button>
      </div>
      <ErrorBox error={error} />
    </div>
  );
}
