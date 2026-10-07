"use client";

import { useEffect, useState } from "react";
import { fmtDateTime, type AgentDetail } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import { useMe } from "@/components/Shell";
import {
  COACHING_STATUS,
  fmtScore,
  isSupervisor,
  patch,
  scoreTone,
  type CoachingItem,
  type CoachingProfile,
} from "@/lib/quality-types";

/** Mi coaching: sugerencias de la IA a partir de las revisiones de calidad y el perfil por criterio. */
export default function CoachingPage() {
  const me = useMe();
  const supervisor = isSupervisor(me?.role);
  const [agentId, setAgentId] = useState<number | null>(null);
  const [days, setDays] = useState(30);
  useEffect(() => {
    const a = new URLSearchParams(window.location.search).get("agent");
    if (a) setAgentId(Number(a));
  }, []);
  const target = agentId ?? me?.id ?? null;
  const agents = useApi<AgentDetail[]>(supervisor ? "/api/agents" : null);
  const profile = useApi<CoachingProfile>(target ? `/api/quality/coaching/profile?agent_id=${target}&days=${days}` : null);
  const items = useApi<CoachingItem[]>(target ? `/api/quality/coaching?agent_id=${target}&status=` : null);
  const [run, busy, error] = useAction();

  async function mark(item: CoachingItem, status: CoachingItem["status"]) {
    if (await run(() => patch(`/api/quality/coaching/${item.id}`, { status }))) {
      items.reload();
      profile.reload();
    }
  }

  const p = profile.data;
  const trend = (n: number | null) =>
    n == null ? null : <span style={{ color: n >= 0 ? "var(--ok)" : "var(--bad)" }}>{n >= 0 ? "▲" : "▼"} {Math.abs(n)}</span>;

  return (
    <>
      <PageHeader
        title={supervisor && target !== me?.id ? `Coaching de ${p?.agent_name ?? "asesor"}` : "Mi coaching"}
        subtitle="Lo que la revisión de calidad encontró en tus conversaciones y cómo mejorarlo."
        actions={
          <span className="inline">
            {supervisor && (
              <select value={target ?? ""} onChange={(e) => setAgentId(Number(e.target.value))}>
                {(agents.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            )}
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              <option value={7}>Últimos 7 días</option>
              <option value={30}>Últimos 30 días</option>
              <option value={90}>Últimos 90 días</option>
            </select>
          </span>
        }
      />
      {profile.error && <ErrorBox error={profile.error} />}
      {error && <ErrorBox error={error} />}
      {!p ? <Loading /> : (
        <>
          <div className="stats">
            <div className="stat">
              <span className="stat-label">Puntaje promedio</span>
              <span className="stat-value">{fmtScore(p.avg_score)}</span>
              <span className="stat-hint">{trend(p.trend)} vs. periodo anterior</span>
            </div>
            <div className="stat">
              <span className="stat-label">Conversaciones revisadas</span>
              <span className="stat-value">{p.reviews}</span>
            </div>
            <div className={`stat ${p.critical_failed ? "bad" : ""}`}>
              <span className="stat-label">Fallas críticas</span>
              <span className="stat-value">{p.critical_failed}</span>
            </div>
          </div>

          {p.weaknesses.length > 0 && (
            <Card title="Dónde enfocarte">
              <div style={{ display: "grid", gap: 12 }}>
                {p.weaknesses.map((w) => (
                  <div key={w.key} className="stat" style={{ gap: 4 }}>
                    <div className="row">
                      <span className="strong">{w.label}</span>
                      <span className="inline"><Badge tone={scoreTone(w.avg)}>{fmtScore(w.avg)}</Badge> {trend(w.trend)}</span>
                    </div>
                    {w.examples.map((e) => (
                      <div key={e.review_id} className="small">
                        <span className="muted">Conversación #{e.conversation_id} ({e.score}):</span>{" "}
                        {e.evidence && <em>«{e.evidence}»</em>} {e.comment}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card title="Sugerencias">
            {!items.data ? <Loading /> : items.data.length === 0 ? <Empty>Sin sugerencias por ahora. ¡Buen trabajo!</Empty> : (
              <div style={{ display: "grid", gap: 10 }}>
                {items.data.map((i) => (
                  <div key={i.id} className="stat" style={{ gap: 4, opacity: i.status === "open" ? 1 : 0.65 }}>
                    <div className="row">
                      <span className="strong">{i.title}</span>
                      <span className="inline">
                        <Badge tone={i.status === "open" ? "warn" : "neutral"}>{COACHING_STATUS[i.status]}</Badge>
                        <span className="muted small">{fmtDateTime(i.created_at)}</span>
                      </span>
                    </div>
                    <div>{i.suggestion}</div>
                    {i.example && <div className="muted small">Ejemplo: «{i.example}»</div>}
                    {i.status !== "done" && i.status !== "dismissed" && (
                      <div className="inline" style={{ gap: 6 }}>
                        {i.status === "open" && <button disabled={busy} onClick={() => mark(i, "acknowledged")}>Entendido</button>}
                        <button className="primary" disabled={busy} onClick={() => mark(i, "done")}>Ya lo aplico</button>
                        <button disabled={busy} onClick={() => mark(i, "dismissed")}>Descartar</button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Por criterio">
            {p.criteria.length === 0 ? <Empty>Aún no hay revisiones en este periodo</Empty> : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr><th>Criterio</th><th className="num">Promedio</th><th className="num">Tendencia</th><th className="num">Revisiones</th></tr>
                  </thead>
                  <tbody>
                    {p.criteria.map((c) => (
                      <tr key={c.key}>
                        <td>{c.label}</td>
                        <td className="num"><Badge tone={scoreTone(c.avg)}>{fmtScore(c.avg)}</Badge></td>
                        <td className="num">{trend(c.trend) ?? "—"}</td>
                        <td className="num">{c.samples}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  );
}
