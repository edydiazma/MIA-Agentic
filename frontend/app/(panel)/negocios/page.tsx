"use client";

import { useState } from "react";
import Link from "next/link";
import { contactLabel, fmtDate, qs, send, type AgentDetail } from "@/lib/api";
import {
  DEAL_STATUS_LABEL,
  DEAL_STATUS_TONE,
  fmtMoney,
  type Deal,
  type DealBoard,
  type PipelinesConfig,
} from "@/lib/crm-types";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Tabs, useAction, useApi } from "@/components/ui";
import DealForm, { LoseDialog } from "@/components/crm/DealForm";
import PipelineEditor from "@/components/crm/PipelineEditor";
import { useIsAdmin } from "@/components/config/common";

type View = "board" | "list";
type ModalState = { kind: "new" } | { kind: "edit" | "lose"; deal: Deal } | { kind: "pipelines" } | null;

export default function NegociosPage() {
  const isAdmin = useIsAdmin();
  const [view, setView] = useState<View>("board");
  const [pipeline, setPipeline] = useState("default");
  const [q, setQ] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [status, setStatus] = useState("open");
  const [modal, setModal] = useState<ModalState>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [run, busy, error] = useAction();

  const config = useApi<PipelinesConfig>("/api/deals/pipelines");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const board = useApi<DealBoard>(view === "board" ? `/api/deals/board${qs({ pipeline })}` : null);
  const list = useApi<Deal[]>(
    view === "list" ? `/api/deals${qs({ pipeline, q: q.trim(), owner_id: ownerId, status, limit: 300 })}` : null,
  );
  const reload = () => (view === "board" ? board.reload() : list.reload());
  const saved = () => {
    setModal(null);
    reload();
  };

  const stages = config.data?.pipelines[pipeline]?.stages ?? board.data?.stages ?? [];
  const stageLabel = (key: string) => stages.find((s) => s.key === key)?.label ?? key;

  // Filtros del tablero aplicados en el cliente (el endpoint del tablero solo filtra por embudo).
  const matches = (d: Deal) =>
    (!ownerId || String(d.owner?.id ?? "") === ownerId) &&
    (!q.trim() ||
      `${d.name} ${d.contact.name ?? ""} ${d.contact.wa_id}`.toLowerCase().includes(q.trim().toLowerCase()));

  async function moveTo(dealId: number, stage: string) {
    const deal = Object.values(board.data?.columns ?? {}).flat().find((d) => d.id === dealId);
    if (!deal || deal.stage === stage) return;
    if (stage === "lost") {
      setModal({ kind: "lose", deal });
      return;
    }
    // Movimiento optimista; si falla se recarga el tablero.
    board.setData((b) =>
      b && {
        ...b,
        columns: Object.fromEntries(
          Object.entries(b.columns).map(([k, ds]) => [
            k,
            k === stage ? [{ ...deal, stage }, ...ds] : ds.filter((d) => d.id !== dealId),
          ]),
        ),
      },
    );
    const r = await run(() =>
      stage === "won"
        ? send<Deal>(`/api/deals/${dealId}/win`, "POST", {})
        : send<Deal>(`/api/deals/${dealId}`, "PUT", { stage }),
    );
    board.reload();
    return r;
  }

  const pipelines = Object.entries(config.data?.pipelines ?? {});

  return (
    <>
      <PageHeader
        title="Negocios"
        subtitle="Oportunidades de venta por etapa. Se sincronizan con HubSpot o Salesforce si están conectados."
        actions={
          <>
            {isAdmin && <button onClick={() => setModal({ kind: "pipelines" })}>Etapas</button>}
            <button className="primary" onClick={() => setModal({ kind: "new" })}>
              + Nuevo negocio
            </button>
          </>
        }
      />
      <Tabs<View>
        value={view}
        onChange={setView}
        tabs={[
          ["board", "Tablero"],
          ["list", "Lista"],
        ]}
      />
      <div className="list-tools">
        {pipelines.length > 1 && (
          <select value={pipeline} onChange={(e) => setPipeline(e.target.value)} aria-label="Embudo">
            {pipelines.map(([k, p]) => (
              <option key={k} value={k}>
                {p.label}
              </option>
            ))}
          </select>
        )}
        <input placeholder="Buscar negocio o cliente…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={ownerId} onChange={(e) => setOwnerId(e.target.value)} aria-label="Responsable">
          <option value="">Todos los responsables</option>
          {(agents.data ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        {view === "list" && (
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Estado">
            <option value="">Todos los estados</option>
            <option value="open">Abiertos</option>
            <option value="won">Ganados</option>
            <option value="lost">Perdidos</option>
          </select>
        )}
      </div>
      <ErrorBox error={error ?? config.error ?? board.error ?? list.error} />

      {view === "board" &&
        (!board.data ? (
          <Loading />
        ) : (
          <div style={{ display: "flex", gap: 12, overflowX: "auto", paddingBottom: 8, alignItems: "flex-start" }}>
            {board.data.stages.map((s) => {
              const deals = (board.data!.columns[s.key] ?? []).filter(matches);
              const total = deals.reduce((acc, d) => acc + (d.amount ?? 0), 0);
              return (
                <div
                  key={s.key}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOver(s.key);
                  }}
                  onDragLeave={() => setOver((o) => (o === s.key ? null : o))}
                  onDrop={(e) => {
                    e.preventDefault();
                    setOver(null);
                    const id = Number(e.dataTransfer.getData("text/plain"));
                    if (id) moveTo(id, s.key);
                  }}
                  style={{
                    flex: "0 0 260px",
                    background: over === s.key ? "var(--accent-soft)" : "var(--bg)",
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius)",
                    padding: 8,
                    minHeight: 200,
                  }}
                >
                  <div className="inline" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                    <span className="strong">
                      {s.label} <span className="muted small">({deals.length})</span>
                    </span>
                    <span className="small muted">{fmtMoney(total, board.data!.currency)}</span>
                  </div>
                  <div className="stack" style={{ gap: 8 }}>
                    {deals.map((d) => (
                      <div
                        key={d.id}
                        draggable={!busy}
                        onDragStart={(e) => {
                          e.dataTransfer.setData("text/plain", String(d.id));
                          setDragging(d.id);
                        }}
                        onDragEnd={() => setDragging(null)}
                        onClick={() => setModal({ kind: "edit", deal: d })}
                        style={{
                          background: "var(--panel)",
                          border: "1px solid var(--border)",
                          borderRadius: "var(--radius)",
                          padding: 10,
                          cursor: "grab",
                          opacity: dragging === d.id ? 0.5 : 1,
                          boxShadow: "var(--shadow)",
                        }}
                      >
                        <div className="strong">{d.name}</div>
                        <div className="small">{fmtMoney(d.amount, d.currency)}</div>
                        <div className="small muted">
                          {contactLabel(d.contact)}
                          {d.owner && ` · ${d.owner.name}`}
                        </div>
                        {d.expected_close && <div className="small muted">Cierre: {fmtDate(d.expected_close)}</div>}
                        {d.crm_links.length > 0 && (
                          <div className="small muted">
                            {d.crm_links.map((l) => (l.provider === "hubspot" ? "HubSpot" : "Salesforce")).join(" · ")} ✓
                          </div>
                        )}
                      </div>
                    ))}
                    {!deals.length && <div className="small muted">Arrastra negocios aquí.</div>}
                  </div>
                </div>
              );
            })}
          </div>
        ))}

      {view === "list" && (
        <Card>
          {!list.data ? (
            <Loading />
          ) : !list.data.length ? (
            <Empty>No hay negocios con estos filtros.</Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Negocio</th>
                    <th>Cliente</th>
                    <th>Etapa</th>
                    <th>Estado</th>
                    <th className="num">Monto</th>
                    <th>Responsable</th>
                    <th>Cierre esperado</th>
                    <th>CRM</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {list.data.map((d) => (
                    <tr key={d.id}>
                      <td>
                        <button className="link strong" onClick={() => setModal({ kind: "edit", deal: d })}>
                          {d.name}
                        </button>
                      </td>
                      <td>
                        {d.conversation_id ? (
                          <Link href={`/conversaciones?id=${d.conversation_id}`}>{contactLabel(d.contact)}</Link>
                        ) : (
                          contactLabel(d.contact)
                        )}
                      </td>
                      <td>{stageLabel(d.stage)}</td>
                      <td>
                        <Badge tone={DEAL_STATUS_TONE[d.status]}>{DEAL_STATUS_LABEL[d.status]}</Badge>
                        {d.lost_reason && <div className="small muted">{d.lost_reason}</div>}
                      </td>
                      <td className="num">{fmtMoney(d.amount, d.currency)}</td>
                      <td>{d.owner?.name ?? "—"}</td>
                      <td className="nowrap">{d.status === "open" ? fmtDate(d.expected_close) : fmtDate(d.closed_at)}</td>
                      <td className="small">
                        {d.crm_links.map((l) => (l.provider === "hubspot" ? "HubSpot" : "Salesforce")).join(", ") || "—"}
                      </td>
                      <td className="right nowrap">
                        {d.status === "open" ? (
                          <>
                            <button
                              className="link small"
                              disabled={busy}
                              onClick={async () => {
                                if (await run(() => send<Deal>(`/api/deals/${d.id}/win`, "POST", {}))) list.reload();
                              }}
                            >
                              Ganado
                            </button>{" "}
                            <button className="link small" onClick={() => setModal({ kind: "lose", deal: d })}>
                              Perdido
                            </button>
                          </>
                        ) : (
                          <button
                            className="link small"
                            disabled={busy}
                            onClick={async () => {
                              if (await run(() => send<Deal>(`/api/deals/${d.id}/reopen`, "POST", {}))) list.reload();
                            }}
                          >
                            Reabrir
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {modal?.kind === "new" && <DealForm pipeline={pipeline} onClose={() => setModal(null)} onSaved={saved} />}
      {modal?.kind === "edit" && <DealForm deal={modal.deal} onClose={() => setModal(null)} onSaved={saved} />}
      {modal?.kind === "lose" && (
        <LoseDialog
          deal={modal.deal}
          onClose={() => {
            setModal(null);
            reload();
          }}
          onSaved={saved}
        />
      )}
      {modal?.kind === "pipelines" && config.data && (
        <PipelineEditor
          config={config.data}
          onClose={() => setModal(null)}
          onSaved={(c) => {
            config.setData(c);
            setModal(null);
            reload();
          }}
        />
      )}
    </>
  );
}
