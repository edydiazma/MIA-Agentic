"use client";

import { useState } from "react";
import type { PlatformPlan } from "@/lib/saas-types";
import { Badge, Card, ErrorBox, Field, Loading, Modal, PageHeader, Toggle } from "@/components/ui";
import { papi, usePlatform } from "@/components/saas/platform-api";

type Draft = Omit<PlatformPlan, "id" | "limits" | "features"> & {
  id?: number;
  limitsText: string;
  featuresText: string;
};

const toDraft = (p?: PlatformPlan): Draft => ({
  id: p?.id,
  key: p?.key ?? "",
  name: p?.name ?? "",
  description: p?.description ?? "",
  price_month_usd: p?.price_month_usd ?? 0,
  provider_price_id: p?.provider_price_id ?? "",
  is_public: p?.is_public ?? true,
  position: p?.position ?? 0,
  limitsText: JSON.stringify(p?.limits ?? {}, null, 2),
  featuresText: JSON.stringify(p?.features ?? {}, null, 2),
});

export default function PlansPage() {
  const { data, error, reload } = usePlatform<PlatformPlan[]>("/plans");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  async function save() {
    if (!draft) return;
    setSaveError(null);
    let limits: unknown, features: unknown;
    try {
      limits = JSON.parse(draft.limitsText);
      features = JSON.parse(draft.featuresText);
    } catch {
      return setSaveError("Límites y funcionalidades deben ser JSON válido.");
    }
    const { id, limitsText: _l, featuresText: _f, ...rest } = draft;
    const body = { ...rest, provider_price_id: rest.provider_price_id || null, limits, features };
    setBusy(true);
    try {
      await (id ? papi(`/plans/${id}`, "PUT", body) : papi("/plans", "POST", body));
      setDraft(null);
      reload();
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Planes"
        subtitle="Precios, límites y funcionalidades. Los cambios aplican de inmediato a todas las empresas del plan."
        actions={
          <button className="primary" onClick={() => setDraft(toDraft())}>
            Nuevo plan
          </button>
        }
      />
      <ErrorBox error={error} />
      {!data ? (
        <Loading />
      ) : (
        <Card>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Plan</th>
                  <th className="num">Precio</th>
                  <th>Precio en Stripe</th>
                  <th>Visibilidad</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <strong>{p.name}</strong> <span className="small muted">{p.key}</span>
                    </td>
                    <td className="num">US$ {p.price_month_usd.toLocaleString("es")}</td>
                    <td className="small">{p.provider_price_id ?? <span className="muted">Sin configurar</span>}</td>
                    <td>{p.is_public ? <Badge tone="ok">Público</Badge> : <Badge>Oculto</Badge>}</td>
                    <td>
                      <button onClick={() => setDraft(toDraft(p))}>Editar</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {draft && (
        <Modal
          wide
          title={draft.id ? `Editar ${draft.name}` : "Nuevo plan"}
          onClose={() => setDraft(null)}
          footer={
            <button className="primary" onClick={save} disabled={busy}>
              {busy ? "Guardando…" : "Guardar"}
            </button>
          }
        >
          <div className="grid2">
            <Field label="Clave" hint={draft.id ? "No se puede cambiar" : "p. ej. team, professional"}>
              <input value={draft.key} disabled={!!draft.id} onChange={(e) => setDraft({ ...draft, key: e.target.value })} />
            </Field>
            <Field label="Nombre">
              <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </Field>
            <Field label="Precio mensual (US$)">
              <input
                type="number"
                min={0}
                value={draft.price_month_usd}
                onChange={(e) => setDraft({ ...draft, price_month_usd: Number(e.target.value) })}
              />
            </Field>
            <Field label="ID de precio en Stripe" hint="price_… — sin esto el plan no se puede comprar en línea">
              <input
                value={draft.provider_price_id ?? ""}
                onChange={(e) => setDraft({ ...draft, provider_price_id: e.target.value })}
              />
            </Field>
            <Field label="Orden">
              <input
                type="number"
                value={draft.position}
                onChange={(e) => setDraft({ ...draft, position: Number(e.target.value) })}
              />
            </Field>
            <Field label="Visible en registro y precios">
              <Toggle checked={draft.is_public} onChange={(v) => setDraft({ ...draft, is_public: v })} />
            </Field>
          </div>
          <Field label="Descripción">
            <input value={draft.description ?? ""} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          </Field>
          <div className="grid2">
            <Field label="Límites (JSON)" hint="null = ilimitado">
              <textarea rows={10} value={draft.limitsText} onChange={(e) => setDraft({ ...draft, limitsText: e.target.value })} />
            </Field>
            <Field label="Funcionalidades (JSON)" hint="true/false por funcionalidad">
              <textarea
                rows={10}
                value={draft.featuresText}
                onChange={(e) => setDraft({ ...draft, featuresText: e.target.value })}
              />
            </Field>
          </div>
          <ErrorBox error={saveError} />
        </Modal>
      )}
    </>
  );
}
