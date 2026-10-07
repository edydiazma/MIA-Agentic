"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { AIAgent, AIAgentIn, Cortex } from "@/lib/ai-types";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";
import AgentEditor from "@/components/ai/AgentEditor";

type Selection = number | "new" | null;

export default function AgentsPage() {
  const isAdmin = useIsAdmin();
  const agents = useApi<AIAgent[]>("/api/bots");
  const cortexes = useApi<Cortex[]>("/api/ai/cortexes");
  const [selected, setSelected] = useState<Selection>(null);
  const [template, setTemplate] = useState<AIAgentIn | null>(null);

  const list = agents.data ?? [];
  useEffect(() => {
    if (selected === null && list.length) setSelected(list[0].id);
  }, [list, selected]);

  const cortexName = (id: number | null) =>
    id ? (cortexes.data ?? []).find((c) => c.id === id)?.name ?? `#${id}` : "Automático";
  const current = typeof selected === "number" ? list.find((a) => a.id === selected) ?? null : null;

  function duplicate(a: AIAgent) {
    const { id: _id, channel_ids: _ch, ...rest } = a;
    setTemplate({ ...rest, name: `${a.name} (copia)`, enabled: false });
    setSelected("new");
  }

  return (
    <>
      <PageHeader
        title="Cortex · Agentes de IA"
        subtitle={
          <>
            Cada agente usa un Cortex (conexiones con failover) y los recursos que actives. Gestiona las conexiones en{" "}
            <Link href="/automatizaciones/cortex/conexiones">Conexiones y failover</Link>.
          </>
        }
        actions={
          isAdmin ? (
            <button
              className="primary"
              onClick={() => {
                setTemplate(null);
                setSelected("new");
              }}
            >
              Nuevo agente
            </button>
          ) : undefined
        }
      />
      <AdminNotice />
      <ErrorBox error={agents.error} />
      {agents.loading && !agents.data ? (
        <Loading />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(240px, 300px) minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
          <Card title="Agentes">
            {list.length === 0 ? (
              <Empty>Aún no hay agentes de IA.</Empty>
            ) : (
              <div className="stack" style={{ gap: 6 }}>
                {list.map((a) => (
                  <div
                    key={a.id}
                    className="card"
                    style={{ padding: 10, cursor: "pointer", marginTop: 0,
                             borderColor: selected === a.id ? "var(--accent)" : undefined }}
                    onClick={() => setSelected(a.id)}
                  >
                    <div className="row">
                      <strong>{a.name}</strong>
                      <Badge tone={a.enabled ? "ok" : "neutral"}>{a.enabled ? "Activo" : "Inactivo"}</Badge>
                    </div>
                    <div className="small muted">
                      Cortex: {cortexName(a.cortex_id)} · {a.channel_ids.length} canal(es)
                    </div>
                    <div className="small muted">
                      {[
                        a.use_knowledge && "conocimiento",
                        a.use_memory && "memoria",
                        a.use_customer_memory && "memoria cliente",
                        a.use_catalog && "catálogo",
                        a.use_appointments && "citas",
                      ].filter(Boolean).join(" · ") || "sin recursos"}
                    </div>
                    {isAdmin && (
                      <button
                        className="link small"
                        onClick={(e) => {
                          e.stopPropagation();
                          duplicate(a);
                        }}
                      >
                        Duplicar
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
          <div>
            {selected === null ? (
              <Empty>Selecciona un agente o crea uno nuevo.</Empty>
            ) : (
              <AgentEditor
                key={String(selected) + (template?.name ?? "")}
                agent={current}
                template={selected === "new" ? template : null}
                isAdmin={isAdmin}
                onSaved={async (a) => {
                  await agents.reload();
                  setTemplate(null);
                  setSelected(a.id);
                }}
              />
            )}
          </div>
        </div>
      )}
    </>
  );
}
