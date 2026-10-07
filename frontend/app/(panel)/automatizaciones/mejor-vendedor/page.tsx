"use client";

import { useState } from "react";
import Link from "next/link";
import { daysAgo, fmtDateTime, fmtPct, isoDay, qs, send } from "@/lib/api";
import type { LearningRun, Playbook, SellerProfile, SellerRanking } from "@/lib/ai-types";
import { Badge, Card, DateRange, Empty, ErrorBox, Field, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";
import RunsList from "@/components/ai/RunsList";

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h3 style={{ fontSize: 13, margin: "8px 0 4px" }}>{title}</h3>
      <ul style={{ margin: 0, paddingLeft: 18 }}>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

function PlaybookView({ p }: { p: Playbook }) {
  return (
    <div className="stack" style={{ gap: 8 }}>
      {p.persona && <div><strong>Persona: </strong>{p.persona}</div>}
      {p.tone && <div><strong>Tono: </strong>{p.tone}</div>}
      {!!p.sales_process?.length && (
        <div>
          <h3 style={{ fontSize: 13, margin: "8px 0 4px" }}>Proceso de venta</h3>
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {p.sales_process.map((s, i) => (
              <li key={i}>
                <strong>{s.stage}</strong> — {s.goal}
                {s.tactics?.length > 0 && <div className="small muted">{s.tactics.join(" · ")}</div>}
              </li>
            ))}
          </ol>
        </div>
      )}
      <List title="Preguntas de descubrimiento" items={p.discovery_questions} />
      {!!p.objections?.length && (
        <div>
          <h3 style={{ fontSize: 13, margin: "8px 0 4px" }}>Objeciones → respuestas</h3>
          <table className="table">
            <tbody>
              {p.objections.map((o, i) => (
                <tr key={i}>
                  <td style={{ width: "35%" }}><em>«{o.objection}»</em></td>
                  <td>{o.response}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <List title="Técnicas de cierre" items={p.closing_techniques} />
      <div className="grid2">
        <List title="✔ Haz" items={p.do} />
        <List title="✘ No hagas" items={p.dont} />
      </div>
      <List title="Frases que funcionan" items={p.example_phrases} />
    </div>
  );
}

function ProfileModal({ profile, isAdmin, onClose, onChanged }: {
  profile: SellerProfile; isAdmin: boolean; onClose: () => void; onChanged: () => void;
}) {
  const [prompt, setPrompt] = useState(profile.system_prompt);
  const [name, setName] = useState(profile.name);
  const [created, setCreated] = useState<number | null>(profile.ai_agent_id);
  const [run, busy, error] = useAction();

  async function save() {
    if ((await run(() => send(`/api/seller/profiles/${profile.id}`, "PUT", { name, system_prompt: prompt }))) !== undefined) onChanged();
  }
  async function createAgent() {
    await run(() => send(`/api/seller/profiles/${profile.id}`, "PUT", { name, system_prompt: prompt }));
    const r = await run(() => send<{ id?: number; ai_agent_id?: number }>(`/api/seller/profiles/${profile.id}/create-agent`, "POST"));
    if (r) {
      setCreated(r.id ?? r.ai_agent_id ?? null);
      onChanged();
    }
  }

  return (
    <Modal
      wide
      title={`Playbook: ${profile.name}`}
      onClose={onClose}
      footer={
        <>
          {created && <Link href="/automatizaciones/cortex">Ver agente creado →</Link>}
          <span style={{ flex: 1 }} />
          <button onClick={onClose}>Cerrar</button>
          {isAdmin && <button disabled={busy} onClick={save}>Guardar</button>}
          {isAdmin && !created && <button className="primary" disabled={busy} onClick={createAgent}>Crear agente de IA</button>}
        </>
      }
    >
      <div className="small muted">
        Aprendido de {String((profile.stats as { agents?: string[] } | null)?.agents?.join(", ") ?? `${profile.source_agent_ids.length} asesor(es)`)} ·{" "}
        {String((profile.stats as { conversations?: number } | null)?.conversations ?? "?")} conversaciones ganadas
      </div>
      <PlaybookView p={profile.playbook ?? {}} />
      <Field label="Nombre del agente"><input value={name} disabled={!isAdmin} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="Prompt del agente vendedor" hint="Revísalo antes de crear el agente; se crea desactivado para que lo pruebes.">
        <textarea rows={14} value={prompt} disabled={!isAdmin} onChange={(e) => setPrompt(e.target.value)} />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export default function BestSellerPage() {
  const isAdmin = useIsAdmin();
  const [range, setRange] = useState({ start: daysAgo(89), end: isoDay() });
  const ranking = useApi<SellerRanking[]>(`/api/seller/ranking${qs(range)}`);
  const runs = useApi<LearningRun[]>("/api/seller/runs");
  const profiles = useApi<SellerProfile[]>("/api/seller/profiles");
  const [picked, setPicked] = useState<number[]>([]);
  const [name, setName] = useState("Mejor vendedor");
  const [open, setOpen] = useState<SellerProfile | null>(null);
  const [run, busy, error] = useAction();

  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  async function start() {
    const r = await run(() => send("/api/seller/runs", "POST", { agent_ids: picked, name, max_conversations: 60 }));
    if (r !== undefined) runs.reload();
  }

  return (
    <>
      <PageHeader
        title="Mejor vendedor"
        subtitle="La IA estudia las conversaciones ganadas de tus mejores asesores y crea un playbook y un agente vendedor que puedes revisar antes de activarlo."
      />
      <AdminNotice />
      <Card title="Ranking de asesores" actions={<DateRange start={range.start} end={range.end} onChange={(start, end) => setRange({ start, end })} />}>
        <ErrorBox error={ranking.error} />
        {(ranking.data ?? []).length === 0 ? (
          <Empty>{ranking.loading ? "Cargando…" : "No hay conversaciones cerradas por asesores en este periodo."}</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th />
                <th>Asesor</th>
                <th className="num">Ventas</th>
                <th className="num">Cerradas</th>
                <th className="num">Conversión</th>
              </tr>
            </thead>
            <tbody>
              {(ranking.data ?? []).map((r, i) => (
                <tr key={r.agent_id}>
                  <td><input type="checkbox" checked={picked.includes(r.agent_id)} disabled={!isAdmin} onChange={() => toggle(r.agent_id)} /></td>
                  <td>{i < 3 && "🏆 "}{r.name}</td>
                  <td className="num strong">{r.sales}</td>
                  <td className="num">{r.closed}</td>
                  <td className="num">{fmtPct(r.conversion_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {isAdmin && (
          <div className="inline" style={{ marginTop: 12 }}>
            <input style={{ maxWidth: 240 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="Nombre del perfil" />
            <button className="primary" disabled={busy || !name.trim()} onClick={start}>
              ✨ Crear mejor vendedor {picked.length ? `con ${picked.length} asesor(es)` : "(top 2 automático)"}
            </button>
          </div>
        )}
        <ErrorBox error={error} />
      </Card>

      <RunsList runs={runs.data ?? []} reload={() => { runs.reload(); profiles.reload(); }} title="Ejecuciones" />

      <Card title="Perfiles generados">
        {(profiles.data ?? []).length === 0 ? (
          <Empty>Aún no hay perfiles.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Perfil</th>
                <th>Estado</th>
                <th>Creado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(profiles.data ?? []).map((p) => (
                <tr key={p.id} className="clickable" onClick={() => setOpen(p)}>
                  <td><strong>{p.name}</strong><div className="small muted">{p.playbook?.persona?.slice(0, 100)}</div></td>
                  <td><Badge tone={p.status === "applied" ? "ok" : "neutral"}>{p.status === "applied" ? "Agente creado" : p.status === "archived" ? "Archivado" : "Borrador"}</Badge></td>
                  <td className="small">{fmtDateTime(p.created_at)}</td>
                  <td><button>Ver</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {open && (
        <ProfileModal profile={open} isAdmin={isAdmin} onClose={() => setOpen(null)} onChanged={profiles.reload} />
      )}
    </>
  );
}
