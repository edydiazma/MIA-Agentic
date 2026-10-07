"use client";

import { useEffect, useState } from "react";
import { contactLabel, qs, send, type AgentDetail, type Contact } from "@/lib/api";
import type { Deal, PipelinesConfig } from "@/lib/crm-types";
import { ErrorBox, Field, Modal, useAction, useApi } from "@/components/ui";

/** Crear o editar un negocio. Si `contact` viene fijo (panel del cliente) no se muestra el buscador. */
export default function DealForm({
  deal,
  contact,
  conversationId,
  pipeline = "default",
  onClose,
  onSaved,
}: {
  deal?: Deal | null;
  contact?: { id: number; name: string | null; wa_id: string } | null;
  conversationId?: number | null;
  pipeline?: string;
  onClose: () => void;
  onSaved: (d: Deal) => void;
}) {
  const config = useApi<PipelinesConfig>("/api/deals/pipelines");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const [q, setQ] = useState("");
  const contacts = useApi<{ total: number; items: Contact[] }>(
    !deal && !contact && q.trim().length >= 2 ? `/api/contacts${qs({ q: q.trim(), limit: 8 })}` : null,
  );
  const [picked, setPicked] = useState(contact ?? deal?.contact ?? null);
  const [form, setForm] = useState({
    name: deal?.name ?? "",
    amount: deal?.amount != null ? String(deal.amount) : "",
    currency: deal?.currency ?? "",
    pipeline: deal?.pipeline ?? pipeline,
    stage: deal?.stage ?? "",
    owner_agent_id: deal?.owner?.id ? String(deal.owner.id) : "",
    expected_close: deal?.expected_close ?? "",
  });
  const [run, busy, error] = useAction();
  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const stages = config.data?.pipelines[form.pipeline]?.stages ?? [];
  useEffect(() => {
    if (!form.currency && config.data) set("currency", config.data.currency);
  }, [config.data, form.currency]);

  async function save() {
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      amount: form.amount === "" ? null : Number(form.amount),
      pipeline: form.pipeline,
      owner_agent_id: form.owner_agent_id ? Number(form.owner_agent_id) : null,
      expected_close: form.expected_close || null,
    };
    if (form.currency) body.currency = form.currency;
    // Solo se envía la etapa si cambió: así no se repiten eventos de ganado/perdido.
    if (form.stage && form.stage !== deal?.stage) body.stage = form.stage;
    const d = await run(() =>
      deal
        ? send<Deal>(`/api/deals/${deal.id}`, "PUT", body)
        : send<Deal>("/api/deals", "POST", {
            ...body,
            contact_id: picked!.id,
            conversation_id: conversationId ?? null,
          }),
    );
    if (d) onSaved(d);
  }

  return (
    <Modal
      title={deal ? "Editar negocio" : "Nuevo negocio"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !form.name.trim() || !picked} onClick={save}>
            {deal ? "Guardar" : "Crear negocio"}
          </button>
        </>
      }
    >
      <div className="form">
        <Field label="Cliente">
          {picked ? (
            <div className="inline">
              <span className="strong">{contactLabel(picked)}</span>
              {!deal && !contact && (
                <button className="link small" onClick={() => setPicked(null)}>
                  Cambiar
                </button>
              )}
            </div>
          ) : (
            <div className="stack" style={{ gap: 4 }}>
              <input autoFocus placeholder="Buscar por nombre o teléfono…" value={q} onChange={(e) => setQ(e.target.value)} />
              {(contacts.data?.items ?? []).map((c) => (
                <button key={c.id} className="link small" style={{ textAlign: "left" }} onClick={() => setPicked(c)}>
                  {contactLabel(c)} <span className="muted">+{c.wa_id}</span>
                </button>
              ))}
            </div>
          )}
        </Field>
        <Field label="Nombre del negocio" hint="Ej. Onix Premier 2026, Plan anual">
          <input value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <div className="grid2">
          <Field label="Monto">
            <input type="number" min={0} step="any" value={form.amount} onChange={(e) => set("amount", e.target.value)} />
          </Field>
          <Field label="Moneda">
            <input maxLength={3} value={form.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} />
          </Field>
        </div>
        <div className="grid2">
          {config.data && Object.keys(config.data.pipelines).length > 1 && (
            <Field label="Embudo">
              <select value={form.pipeline} onChange={(e) => setForm((f) => ({ ...f, pipeline: e.target.value, stage: "" }))}>
                {Object.entries(config.data.pipelines).map(([k, p]) => (
                  <option key={k} value={k}>
                    {p.label}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label="Etapa">
            <select value={form.stage} onChange={(e) => set("stage", e.target.value)}>
              {!deal && <option value="">{stages[0]?.label ?? "Primera etapa"}</option>}
              {stages.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="grid2">
          <Field label="Responsable">
            <select value={form.owner_agent_id} onChange={(e) => set("owner_agent_id", e.target.value)}>
              <option value="">{deal ? "Sin responsable" : "Yo"}</option>
              {(agents.data ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Cierre esperado">
            <input type="date" value={form.expected_close} onChange={(e) => set("expected_close", e.target.value)} />
          </Field>
        </div>
        <ErrorBox error={error ?? config.error} />
      </div>
    </Modal>
  );
}

/** Pide el motivo de pérdida y marca el negocio como perdido. */
export function LoseDialog({ deal, onClose, onSaved }: { deal: Deal; onClose: () => void; onSaved: (d: Deal) => void }) {
  const [reason, setReason] = useState("");
  const [run, busy, error] = useAction();
  async function lose() {
    const d = await run(() => send<Deal>(`/api/deals/${deal.id}/lose`, "POST", { reason: reason.trim() || null }));
    if (d) onSaved(d);
  }
  return (
    <Modal
      title={`Marcar «${deal.name}» como perdido`}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="danger" disabled={busy} onClick={lose}>
            Marcar perdido
          </button>
        </>
      }
    >
      <Field label="Motivo" hint="Ej. Precio, Compró en otro lugar, Sin respuesta">
        <input autoFocus value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}
