"use client";

import { useState } from "react";
import { qs, send } from "@/lib/api";
import { DEAL_STATUS_LABEL, DEAL_STATUS_TONE, fmtMoney, type Deal, type PipelinesConfig } from "@/lib/crm-types";
import { Badge, ErrorBox, useAction, useApi } from "@/components/ui";
import DealForm, { LoseDialog } from "./DealForm";

/** Negocios del cliente (panel de conversación y ficha del cliente). */
export default function DealsSection({
  contact,
  conversationId,
  heading = "h3",
}: {
  contact: { id: number; name: string | null; wa_id: string };
  conversationId?: number | null;
  heading?: "h2" | "h3";
}) {
  const deals = useApi<Deal[]>(`/api/deals${qs({ contact_id: contact.id, limit: 20 })}`);
  const config = useApi<PipelinesConfig>("/api/deals/pipelines");
  const [modal, setModal] = useState<{ kind: "new" } | { kind: "edit" | "lose"; deal: Deal } | null>(null);
  const [run, busy, error] = useAction();
  const H = heading;

  const stageLabel = (d: Deal) =>
    config.data?.pipelines[d.pipeline]?.stages.find((s) => s.key === d.stage)?.label ?? d.stage;
  const saved = () => {
    setModal(null);
    deals.reload();
  };
  const act = async (d: Deal, path: "win" | "reopen") => {
    if (await run(() => send<Deal>(`/api/deals/${d.id}/${path}`, "POST", {}))) deals.reload();
  };

  return (
    <div>
      <div className="inline" style={{ justifyContent: "space-between" }}>
        <H style={{ marginBottom: 6 }}>Negocios</H>
        <button className="link small" onClick={() => setModal({ kind: "new" })}>
          + Nuevo
        </button>
      </div>
      {!deals.data?.length ? (
        <p className="muted small" style={{ margin: 0 }}>
          Sin negocios.
        </p>
      ) : (
        deals.data.map((d) => (
          <div key={d.id} className="small" style={{ marginBottom: 8 }}>
            <div className="inline" style={{ justifyContent: "space-between" }}>
              <button className="link strong" onClick={() => setModal({ kind: "edit", deal: d })}>
                {d.name}
              </button>
              <Badge tone={DEAL_STATUS_TONE[d.status]}>{d.status === "open" ? stageLabel(d) : DEAL_STATUS_LABEL[d.status]}</Badge>
            </div>
            <div className="muted">
              {fmtMoney(d.amount, d.currency)}
              {d.owner && ` · ${d.owner.name}`}
              {d.crm_links.map((l) => ` · ${l.provider === "hubspot" ? "HubSpot" : "Salesforce"} ✓`).join("")}
            </div>
            <div className="inline small" style={{ gap: 8 }}>
              {d.status === "open" ? (
                <>
                  <button className="link small" disabled={busy} onClick={() => act(d, "win")}>
                    Ganado
                  </button>
                  <button className="link small" disabled={busy} onClick={() => setModal({ kind: "lose", deal: d })}>
                    Perdido
                  </button>
                </>
              ) : (
                <button className="link small" disabled={busy} onClick={() => act(d, "reopen")}>
                  Reabrir
                </button>
              )}
            </div>
          </div>
        ))
      )}
      <ErrorBox error={error ?? deals.error} />
      {modal?.kind === "new" && (
        <DealForm contact={contact} conversationId={conversationId} onClose={() => setModal(null)} onSaved={saved} />
      )}
      {modal?.kind === "edit" && <DealForm deal={modal.deal} onClose={() => setModal(null)} onSaved={saved} />}
      {modal?.kind === "lose" && <LoseDialog deal={modal.deal} onClose={() => setModal(null)} onSaved={saved} />}
    </div>
  );
}
