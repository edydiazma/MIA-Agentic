"use client";

import { useState } from "react";
import Link from "next/link";
import { daysAgo, fmtDateTime, isoDay, qs, send, type AgentDetail, type Group } from "@/lib/api";
import {
  Badge,
  Card,
  DateRange,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageHeader,
  Tabs,
  Toggle,
  useAction,
  useApi,
} from "@/components/ui";
import { useIsAdmin } from "@/components/config/common";
import ReviewDetail from "@/components/quality/ReviewDetail";
import {
  REVIEW_STATUS,
  fmtScore,
  scoreTone,
  type Criterion,
  type Review,
  type ReviewList,
  type Scorecard,
  type ScorecardIn,
} from "@/lib/quality-types";

type Tab = "reviews" | "scorecards";
const APPLIES: Record<Scorecard["applies_to"], string> = { agent: "Asesores", bot: "Bot", any: "Asesor o bot" };

export default function QualityPage() {
  const [tab, setTab] = useState<Tab>("reviews");
  return (
    <>
      <PageHeader
        title="Calidad (QA)"
        subtitle={
          <>
            La IA evalúa las conversaciones cerradas con tus rúbricas, da un puntaje por criterio con evidencia y sugiere
            coaching a cada asesor. Resultados en <Link href="/reportes/calidad">Reportes › Calidad y coaching</Link>.
          </>
        }
      />
      <Tabs value={tab} onChange={setTab} tabs={[["reviews", "Revisiones"], ["scorecards", "Rúbricas"]]} />
      {tab === "reviews" ? <Reviews /> : <Scorecards />}
    </>
  );
}

// --- Revisiones -------------------------------------------------------------------
function Reviews() {
  const [range, setRange] = useState({ start: daysAgo(13), end: isoDay() });
  const [filters, setFilters] = useState({ agent_id: "", scorecard_id: "", status: "", critical: "", subject: "" });
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const agents = useApi<AgentDetail[]>("/api/agents");
  const cards = useApi<Scorecard[]>("/api/quality/scorecards");
  const list = useApi<ReviewList>(`/api/quality/reviews${qs({ ...range, ...filters, limit: 50, offset: page * 50 })}`);
  const set = (k: keyof typeof filters, v: string) => {
    setFilters({ ...filters, [k]: v });
    setPage(0);
  };

  return (
    <Card
      title="Revisiones"
      actions={<DateRange start={range.start} end={range.end} onChange={(start, end) => setRange({ start, end })} />}
    >
      <div className="inline filters" style={{ marginBottom: 12 }}>
        <select value={filters.agent_id} onChange={(e) => set("agent_id", e.target.value)}>
          <option value="">Todos los asesores</option>
          {(agents.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select value={filters.scorecard_id} onChange={(e) => set("scorecard_id", e.target.value)}>
          <option value="">Todas las rúbricas</option>
          {(cards.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select value={filters.subject} onChange={(e) => set("subject", e.target.value)}>
          <option value="">Asesor y bot</option>
          <option value="agent">Asesores</option>
          <option value="bot">Bot</option>
        </select>
        <select value={filters.status} onChange={(e) => set("status", e.target.value)}>
          <option value="">Cualquier estado</option>
          {Object.entries(REVIEW_STATUS).map(([k, [l]]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select value={filters.critical} onChange={(e) => set("critical", e.target.value)}>
          <option value="">Con y sin fallas críticas</option>
          <option value="true">Solo con falla crítica</option>
        </select>
      </div>
      {list.error && <ErrorBox error={list.error} />}
      {!list.data ? <Loading /> : list.data.items.length === 0 ? (
        <Empty>No hay revisiones en este periodo. Se crean al cerrar conversaciones con respuestas de asesores o del bot.</Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Fecha</th>
                  <th>Conversación</th>
                  <th>Evaluado</th>
                  <th>Rúbrica</th>
                  <th className="num">Puntaje</th>
                  <th>Estado</th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((r) => {
                  const [label, tone] = REVIEW_STATUS[r.status];
                  return (
                    <tr key={r.id} className="clickable" onClick={() => setOpen(r.id)}>
                      <td className="small">{fmtDateTime(r.created_at)}</td>
                      <td>#{r.conversation_id} <span className="muted small">{r.contact_name ?? ""}</span></td>
                      <td>{r.subject_type === "agent" ? r.agent_name ?? "Asesor" : `🤖 ${r.ai_agent_name ?? "Bot"}`}</td>
                      <td className="small">
                        {r.scorecard_name} {r.reviewer_type === "human" && <Badge tone="info">Manual</Badge>}
                      </td>
                      <td className="num">
                        <Badge tone={scoreTone(r.total_score)}>{fmtScore(r.total_score)}</Badge>
                        {r.critical_failed && <> <Badge tone="bad">Crítica</Badge></>}
                      </td>
                      <td><Badge tone={tone}>{label}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="inline" style={{ justifyContent: "space-between", marginTop: 8 }}>
            <span className="muted small">{list.data.total} revisiones</span>
            <span className="inline">
              <button disabled={page === 0} onClick={() => setPage(page - 1)}>Anterior</button>
              <button disabled={(page + 1) * 50 >= list.data.total} onClick={() => setPage(page + 1)}>Siguiente</button>
            </span>
          </div>
        </>
      )}
      {open !== null && <ReviewModal id={open} onClose={() => setOpen(null)} onChange={list.reload} />}
    </Card>
  );
}

function ReviewModal({ id, onClose, onChange }: { id: number; onClose: () => void; onChange: () => void }) {
  const review = useApi<Review>(`/api/quality/reviews/${id}`);
  return (
    <Modal title={`Revisión #${id}`} onClose={onClose} wide>
      {review.error && <ErrorBox error={review.error} />}
      {!review.data ? <Loading /> : (
        <ReviewDetail review={review.data} onChange={() => { review.reload(); onChange(); }} />
      )}
    </Modal>
  );
}

// --- Rúbricas ---------------------------------------------------------------------
const EMPTY: ScorecardIn = {
  name: "",
  applies_to: "agent",
  criteria: [{ key: "", label: "", description: "", weight: 50, critical: false }],
  auto_review: true,
  sample_pct: 100,
  min_messages: 3,
  group_ids: [],
  is_active: true,
};

function Scorecards() {
  const isAdmin = useIsAdmin();
  const cards = useApi<Scorecard[]>("/api/quality/scorecards");
  const [editing, setEditing] = useState<{ id: number | null; form: ScorecardIn } | null>(null);
  const [run, busy, error] = useAction();

  async function remove(c: Scorecard) {
    if (!confirm(`¿Eliminar la rúbrica «${c.name}»? Las revisiones hechas se conservan.`)) return;
    if (await run(() => send(`/api/quality/scorecards/${c.id}`, "DELETE"))) cards.reload();
  }

  return (
    <Card
      title="Rúbricas"
      actions={isAdmin && (
        <button className="primary" onClick={() => setEditing({ id: null, form: structuredClone(EMPTY) })}>+ Nueva rúbrica</button>
      )}
    >
      {cards.error && <ErrorBox error={cards.error} />}
      {error && <ErrorBox error={error} />}
      {!cards.data ? <Loading /> : (
        <div style={{ display: "grid", gap: 12 }}>
          {cards.data.map((c) => {
            const total = c.criteria.reduce((a, x) => a + x.weight, 0);
            return (
              <div key={c.id} className="stat" style={{ gap: 6 }}>
                <div className="row">
                  <span className="strong">{c.name}</span>
                  <span className="inline">
                    <Badge tone="neutral">{APPLIES[c.applies_to]}</Badge>
                    {c.is_active ? (
                      <Badge tone={c.auto_review ? "ok" : "neutral"}>
                        {c.auto_review ? `Automática · muestra ${c.sample_pct} %` : "Solo manual"}
                      </Badge>
                    ) : <Badge tone="warn">Inactiva</Badge>}
                    {isAdmin && (
                      <>
                        <button onClick={() => setEditing({ id: c.id, form: { ...c, criteria: c.criteria.map((x) => ({ ...x })) } })}>Editar</button>
                        <button className="danger" disabled={busy} onClick={() => remove(c)}>Eliminar</button>
                      </>
                    )}
                  </span>
                </div>
                <div className="small">
                  {c.criteria.map((x) => (
                    <span key={x.key} style={{ marginRight: 12 }}>
                      {x.label} <span className="muted">{total ? Math.round((100 * x.weight) / total) : 0} %</span>
                      {x.critical && " ⚠️"}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {editing && (
        <ScorecardEditor
          id={editing.id}
          initial={editing.form}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            cards.reload();
          }}
        />
      )}
    </Card>
  );
}

function ScorecardEditor({ id, initial, onClose, onSaved }: {
  id: number | null;
  initial: ScorecardIn;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<ScorecardIn>(initial);
  const groups = useApi<Group[]>("/api/groups");
  const [run, busy, error] = useAction();
  const total = form.criteria.reduce((a, c) => a + (Number(c.weight) || 0), 0);
  const setCrit = (i: number, patch: Partial<Criterion>) =>
    setForm({ ...form, criteria: form.criteria.map((c, j) => (j === i ? { ...c, ...patch } : c)) });

  async function save() {
    const body = { ...form, criteria: form.criteria.map((c) => ({ ...c, weight: Number(c.weight) || 0 })) };
    const ok = await run(() => send(id ? `/api/quality/scorecards/${id}` : "/api/quality/scorecards", id ? "PUT" : "POST", body));
    if (ok) onSaved();
  }

  return (
    <Modal
      title={id ? "Editar rúbrica" : "Nueva rúbrica"}
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !form.name.trim() || total <= 0} onClick={save}>Guardar</button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}
      <div className="grid2">
        <Field label="Nombre">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Atención de asesores" />
        </Field>
        <Field label="Evalúa a">
          <select value={form.applies_to} onChange={(e) => setForm({ ...form, applies_to: e.target.value as Scorecard["applies_to"] })}>
            <option value="agent">Asesores</option>
            <option value="bot">Bot</option>
            <option value="any">Asesor si participó; si no, el bot</option>
          </select>
        </Field>
        <Field label="Muestra (%)" hint="Porcentaje de conversaciones cerradas que se evalúan (cada revisión es una llamada a la IA).">
          <input type="number" min={0} max={100} value={form.sample_pct}
            onChange={(e) => setForm({ ...form, sample_pct: Number(e.target.value) })} />
        </Field>
        <Field label="Mínimo de mensajes">
          <input type="number" min={0} value={form.min_messages}
            onChange={(e) => setForm({ ...form, min_messages: Number(e.target.value) })} />
        </Field>
        <Field label="Grupos" hint="Vacío = todos.">
          <select multiple value={form.group_ids.map(String)} style={{ minHeight: 72 }}
            onChange={(e) => setForm({ ...form, group_ids: Array.from(e.target.selectedOptions, (o) => Number(o.value)) })}>
            {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <div style={{ display: "grid", gap: 8, alignContent: "start", paddingTop: 22 }}>
          <Toggle checked={form.auto_review} onChange={(v) => setForm({ ...form, auto_review: v })} label="Evaluar automáticamente al cerrar" />
          <Toggle checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} label="Activa" />
        </div>
      </div>

      <h3 style={{ margin: "16px 0 8px" }}>Criterios <span className="muted small">(peso total {total})</span></h3>
      <div style={{ display: "grid", gap: 8 }}>
        {form.criteria.map((c, i) => (
          <div key={i} className="stat" style={{ gap: 6 }}>
            <div className="inline" style={{ gap: 8 }}>
              <input style={{ flex: 1 }} placeholder="Nombre del criterio" value={c.label}
                onChange={(e) => setCrit(i, { label: e.target.value })} />
              <input type="number" min={0} max={100} style={{ width: 80 }} title="Peso" value={c.weight}
                onChange={(e) => setCrit(i, { weight: Number(e.target.value) })} />
              <span className="muted small" style={{ width: 44 }}>{total ? Math.round((100 * (Number(c.weight) || 0)) / total) : 0} %</span>
              <Toggle checked={c.critical} onChange={(v) => setCrit(i, { critical: v })} label="Crítico" />
              <button className="icon" title="Quitar" disabled={form.criteria.length === 1}
                onClick={() => setForm({ ...form, criteria: form.criteria.filter((_, j) => j !== i) })}>✕</button>
            </div>
            <input placeholder="Qué se espera (la IA lo usa para puntuar)" value={c.description ?? ""}
              onChange={(e) => setCrit(i, { description: e.target.value })} />
          </div>
        ))}
      </div>
      <button style={{ marginTop: 8 }} disabled={form.criteria.length >= 15}
        onClick={() => setForm({ ...form, criteria: [...form.criteria, { key: "", label: "", description: "", weight: 10, critical: false }] })}>
        + Criterio
      </button>
      <p className="muted small">Un criterio crítico con puntaje menor a 50 marca la revisión como «falla crítica».</p>
    </Modal>
  );
}
