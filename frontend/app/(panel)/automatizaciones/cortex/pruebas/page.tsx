"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { fmtDateTime, send } from "@/lib/api";
import type { AIAgent } from "@/lib/ai-types";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageHeader,
  Toggle,
  useAction,
  useApi,
} from "@/components/ui";
import { useIsAdmin } from "@/components/config/common";
import {
  CHECK_LABEL,
  type TestCase,
  type TestExpectations,
  type TestRun,
  type TestRunDetail,
  type TestSuite,
  type TestTurn,
} from "@/lib/quality-types";

const RUN_STATUS: Record<TestRun["status"], [string, "ok" | "bad" | "warn" | "neutral"]> = {
  running: ["Corriendo…", "neutral"],
  passed: ["Aprobada", "ok"],
  failed: ["No aprobada", "bad"],
  error: ["Error", "warn"],
};
const TRIGGER: Record<TestRun["trigger"], string> = { manual: "Manual", on_change: "Al guardar el agente", schedule: "Programada" };

/** Texto «Cliente: …» / «Agente: …» por línea ⇄ turnos. */
function turnsToText(turns: TestTurn[]): string {
  return turns.map((t) => `${t.role === "user" ? "Cliente" : "Agente"}: ${t.text}`).join("\n");
}
function textToTurns(text: string): TestTurn[] {
  const out: TestTurn[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(/^(cliente|agente|bot|asesor)\s*:\s*(.*)$/i);
    if (m) out.push({ role: m[1].toLowerCase() === "cliente" ? "user" : "assistant", text: m[2] });
    else if (out.length) out[out.length - 1].text += "\n" + line;
    else out.push({ role: "user", text: line });
  }
  return out;
}
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function AgentTestsPage() {
  const isAdmin = useIsAdmin();
  const agents = useApi<AIAgent[]>("/api/bots");
  const [agentId, setAgentId] = useState<number | null>(null);
  const [suiteId, setSuiteId] = useState<number | null>(null);
  useEffect(() => {
    if (agentId === null && agents.data?.length) setAgentId(agents.data[0].id);
  }, [agents.data, agentId]);
  const suites = useApi<TestSuite[]>(agentId ? `/api/agent-tests/suites?ai_agent_id=${agentId}` : null);
  useEffect(() => {
    if (suites.data && !suites.data.some((s) => s.id === suiteId)) setSuiteId(suites.data[0]?.id ?? null);
  }, [suites.data, suiteId]);
  const [newSuite, setNewSuite] = useState(false);

  return (
    <>
      <PageHeader
        title="Pruebas de agentes"
        subtitle={
          <>
            Casos de conversación con lo que el agente debe (y no debe) responder. Se corren en simulación: no se envían
            mensajes ni se agendan citas. Configura los agentes en <Link href="/automatizaciones/cortex">Cortex</Link>.
          </>
        }
        actions={
          <span className="inline">
            <select value={agentId ?? ""} onChange={(e) => { setAgentId(Number(e.target.value)); setSuiteId(null); }}>
              {(agents.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
            {isAdmin && agentId && <button className="primary" onClick={() => setNewSuite(true)}>+ Nueva suite</button>}
          </span>
        }
      />
      {suites.error && <ErrorBox error={suites.error} />}
      {!suites.data ? <Loading /> : suites.data.length === 0 ? (
        <Card><Empty>Este agente no tiene pruebas. Crea una suite y agrega casos (también desde conversaciones reales).</Empty></Card>
      ) : (
        <>
          <nav className="tabs">
            {suites.data.map((s) => (
              <button key={s.id} className={s.id === suiteId ? "tab active" : "tab"} onClick={() => setSuiteId(s.id)}>
                {s.name}{" "}
                {s.last_run && <Badge tone={RUN_STATUS[s.last_run.status][1]}>{s.last_run.pass_pct ?? 0} %</Badge>}
              </button>
            ))}
          </nav>
          {suiteId && <SuiteView key={suiteId} suite={suites.data.find((s) => s.id === suiteId)!} onChange={suites.reload} />}
        </>
      )}
      {newSuite && agentId && (
        <SuiteModal agentId={agentId} onClose={() => setNewSuite(false)} onSaved={(id) => { setNewSuite(false); setSuiteId(id); suites.reload(); }} />
      )}
    </>
  );
}

function SuiteModal({ agentId, suite, onClose, onSaved }: {
  agentId: number;
  suite?: TestSuite;
  onClose: () => void;
  onSaved: (id: number) => void;
}) {
  const [form, setForm] = useState({
    name: suite?.name ?? "",
    description: suite?.description ?? "",
    run_on_change: suite?.run_on_change ?? true,
    min_pass_pct: suite?.min_pass_pct ?? 80,
  });
  const [run, busy, error] = useAction();
  async function save() {
    const body = { ...form, ai_agent_id: agentId };
    const r = await run(() => send<TestSuite>(suite ? `/api/agent-tests/suites/${suite.id}` : "/api/agent-tests/suites",
      suite ? "PUT" : "POST", body));
    if (r) onSaved(r.id);
  }
  return (
    <Modal title={suite ? "Editar suite" : "Nueva suite"} onClose={onClose}
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy || !form.name.trim()} onClick={save}>Guardar</button></>}>
      {error && <ErrorBox error={error} />}
      <Field label="Nombre"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Preguntas frecuentes" /></Field>
      <Field label="Descripción"><input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
      <Field label="Aprobada con (%)">
        <input type="number" min={0} max={100} value={form.min_pass_pct} onChange={(e) => setForm({ ...form, min_pass_pct: Number(e.target.value) })} />
      </Field>
      <Toggle checked={form.run_on_change} onChange={(v) => setForm({ ...form, run_on_change: v })}
        label="Correr cada vez que se guarde el agente (cada caso es una llamada a la IA)" />
    </Modal>
  );
}

function SuiteView({ suite, onChange }: { suite: TestSuite; onChange: () => void }) {
  const isAdmin = useIsAdmin();
  const cases = useApi<TestCase[]>(`/api/agent-tests/suites/${suite.id}/cases`);
  const runs = useApi<TestRun[]>(`/api/agent-tests/suites/${suite.id}/runs`);
  const [editing, setEditing] = useState<TestCase | "new" | null>(null);
  const [fromConv, setFromConv] = useState(false);
  const [editSuite, setEditSuite] = useState(false);
  const [openRun, setOpenRun] = useState<number | null>(null);
  const [run, busy, error] = useAction();
  const running = (runs.data ?? []).some((r) => r.status === "running");

  const reloadRuns = runs.reload;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => { reloadRuns(); }, 3000);
    return () => clearInterval(t);
  }, [running, reloadRuns]);

  async function start() {
    const r = await run(() => send<TestRun>(`/api/agent-tests/suites/${suite.id}/run`, "POST"));
    if (r) { runs.reload(); onChange(); }
  }
  async function removeCase(c: TestCase) {
    if (confirm(`¿Eliminar el caso «${c.name}»?`) && await run(() => send(`/api/agent-tests/cases/${c.id}`, "DELETE"))) cases.reload();
  }
  async function removeSuite() {
    if (confirm(`¿Eliminar la suite «${suite.name}» con sus casos y resultados?`)
      && await run(() => send(`/api/agent-tests/suites/${suite.id}`, "DELETE"))) onChange();
  }

  return (
    <div className="grid2">
      <Card
        title={`Casos (${cases.data?.length ?? 0})`}
        actions={isAdmin && (
          <span className="inline">
            <button onClick={() => setFromConv(true)}>Desde conversación</button>
            <button onClick={() => setEditing("new")}>+ Caso</button>
            <button className="primary" disabled={busy || running || !cases.data?.length} onClick={start}>
              {running ? "Corriendo…" : "▶ Correr"}
            </button>
          </span>
        )}
      >
        {error && <ErrorBox error={error} />}
        <p className="muted small" style={{ marginTop: 0 }}>
          {suite.description} Aprobada con {suite.min_pass_pct} %.{suite.run_on_change && " Corre al guardar el agente."}
          {isAdmin && <> <button className="link small" onClick={() => setEditSuite(true)}>Editar</button> · <button className="link small danger" onClick={removeSuite}>Eliminar</button></>}
        </p>
        {!cases.data ? <Loading /> : cases.data.length === 0 ? <Empty>Agrega el primer caso</Empty> : (
          <div style={{ display: "grid", gap: 8 }}>
            {cases.data.map((c) => (
              <div key={c.id} className="stat" style={{ gap: 4 }}>
                <div className="row">
                  <span className="strong">{c.name}</span>
                  {isAdmin && (
                    <span className="inline">
                      <button className="link small" onClick={() => setEditing(c)}>Editar</button>
                      <button className="link small danger" onClick={() => removeCase(c)}>Eliminar</button>
                    </span>
                  )}
                </div>
                <span className="small muted">«{c.turns[c.turns.length - 1]?.text}»</span>
                <span className="small">{describe(c.expectations)}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Ejecuciones">
        {!runs.data ? <Loading /> : runs.data.length === 0 ? <Empty>Aún no se ha corrido</Empty> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Fecha</th><th>Origen</th><th className="num">Aprobados</th><th>Estado</th><th className="num">Costo</th></tr></thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id} className="clickable" onClick={() => setOpenRun(r.id)}>
                    <td className="small">{fmtDateTime(r.started_at)}</td>
                    <td className="small">{TRIGGER[r.trigger]}</td>
                    <td className="num">{r.passed}/{r.total} {r.pass_pct != null && <span className="muted small">({r.pass_pct} %)</span>}</td>
                    <td><Badge tone={RUN_STATUS[r.status][1]}>{RUN_STATUS[r.status][0]}</Badge></td>
                    <td className="num small">US$ {r.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {editing && (
        <CaseModal suiteId={suite.id} initial={editing === "new" ? null : editing} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); cases.reload(); onChange(); }} />
      )}
      {fromConv && (
        <FromConversationModal suiteId={suite.id} onClose={() => setFromConv(false)}
          onSaved={() => { setFromConv(false); cases.reload(); onChange(); }} />
      )}
      {editSuite && (
        <SuiteModal agentId={suite.ai_agent_id} suite={suite} onClose={() => setEditSuite(false)}
          onSaved={() => { setEditSuite(false); onChange(); }} />
      )}
      {openRun !== null && (
        <RunModal id={openRun} previous={(runs.data ?? []).find((r) => r.id < openRun && r.status !== "running")?.id ?? null}
          onClose={() => setOpenRun(null)} />
      )}
    </div>
  );
}

function describe(e: TestExpectations): string {
  const parts: string[] = [];
  if (e.must_include?.length) parts.push(`Debe incluir: ${e.must_include.join(", ")}`);
  if (e.must_not_include?.length) parts.push(`No debe incluir: ${e.must_not_include.join(", ")}`);
  if (e.expect_handoff !== undefined) parts.push(e.expect_handoff ? "Debe transferir a un asesor" : "No debe transferir");
  if (e.expect_tool) parts.push(`Usa la herramienta ${e.expect_tool}`);
  if (e.rubric) parts.push(`Rúbrica: ${e.rubric}`);
  return parts.join(" · ") || "Sin expectativas (pasa si responde)";
}

function ExpectationsFields({ value, onChange }: { value: TestExpectations; onChange: (v: TestExpectations) => void }) {
  const handoff = value.expect_handoff === undefined ? "" : value.expect_handoff ? "yes" : "no";
  return (
    <>
      <div className="grid2">
        <Field label="Debe incluir" hint="Separado por comas; sin importar mayúsculas ni tildes.">
          <input value={(value.must_include ?? []).join(", ")} onChange={(e) => onChange({ ...value, must_include: list(e.target.value) })} />
        </Field>
        <Field label="No debe incluir">
          <input value={(value.must_not_include ?? []).join(", ")} onChange={(e) => onChange({ ...value, must_not_include: list(e.target.value) })} />
        </Field>
        <Field label="Transferencia a asesor">
          <select value={handoff} onChange={(e) => {
            const v = { ...value };
            if (e.target.value === "") delete v.expect_handoff;
            else v.expect_handoff = e.target.value === "yes";
            onChange(v);
          }}>
            <option value="">No importa</option>
            <option value="yes">Debe transferir</option>
            <option value="no">No debe transferir</option>
          </select>
        </Field>
        <Field label="Herramienta esperada">
          <select value={value.expect_tool ?? ""} onChange={(e) => onChange({ ...value, expect_tool: e.target.value || undefined })}>
            <option value="">Ninguna en particular</option>
            <option value="transfer_to_human">Transferir a asesor</option>
            <option value="check_availability">Consultar disponibilidad</option>
            <option value="book_appointment">Agendar cita</option>
            <option value="search_products">Buscar productos</option>
            <option value="send_product">Enviar producto</option>
          </select>
        </Field>
      </div>
      <Field label="Rúbrica para el juez IA (opcional)" hint="Un LLM puntúa la respuesta de 0 a 100 con este criterio; pasa con 70.">
        <input value={value.rubric ?? ""} placeholder="Pregunta el presupuesto antes de recomendar un modelo"
          onChange={(e) => onChange({ ...value, rubric: e.target.value || undefined })} />
      </Field>
    </>
  );
}

function CaseModal({ suiteId, initial, onClose, onSaved }: {
  suiteId: number;
  initial: TestCase | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [text, setText] = useState(initial ? turnsToText(initial.turns) : "Cliente: ");
  const [exp, setExp] = useState<TestExpectations>(initial?.expectations ?? {});
  const [run, busy, error] = useAction();
  async function save() {
    const body = { name, turns: textToTurns(text), expectations: exp, position: initial?.position ?? 0 };
    const ok = await run(() => send(initial ? `/api/agent-tests/cases/${initial.id}` : `/api/agent-tests/suites/${suiteId}/cases`,
      initial ? "PUT" : "POST", body));
    if (ok) onSaved();
  }
  return (
    <Modal title={initial ? "Editar caso" : "Nuevo caso"} onClose={onClose} wide
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy || !name.trim()} onClick={save}>Guardar</button></>}>
      {error && <ErrorBox error={error} />}
      <Field label="Nombre"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Pregunta por el precio" /></Field>
      <Field label="Conversación" hint="Una línea por mensaje: «Cliente: …» o «Agente: …». El último mensaje debe ser del cliente.">
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      <ExpectationsFields value={exp} onChange={setExp} />
    </Modal>
  );
}

function FromConversationModal({ suiteId, onClose, onSaved }: { suiteId: number; onClose: () => void; onSaved: () => void }) {
  const [conversationId, setConversationId] = useState("");
  const [name, setName] = useState("");
  const [exp, setExp] = useState<TestExpectations>({});
  const [run, busy, error] = useAction();
  async function save() {
    const ok = await run(() => send(`/api/agent-tests/suites/${suiteId}/cases/from-conversation`, "POST",
      { conversation_id: Number(conversationId), name: name || null, expectations: exp }));
    if (ok) onSaved();
  }
  return (
    <Modal title="Caso desde una conversación real" onClose={onClose} wide
      footer={<><button onClick={onClose}>Cancelar</button><button className="primary" disabled={busy || !conversationId} onClick={save}>Crear caso</button></>}>
      {error && <ErrorBox error={error} />}
      <div className="grid2">
        <Field label="Número de conversación" hint="Se toman sus mensajes hasta el último del cliente.">
          <input type="number" value={conversationId} onChange={(e) => setConversationId(e.target.value)} />
        </Field>
        <Field label="Nombre (opcional)"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
      </div>
      <ExpectationsFields value={exp} onChange={setExp} />
    </Modal>
  );
}

function RunModal({ id, previous, onClose }: { id: number; previous: number | null; onClose: () => void }) {
  const detail = useApi<TestRunDetail>(`/api/agent-tests/runs/${id}`);
  const prev = useApi<TestRunDetail>(previous ? `/api/agent-tests/runs/${previous}` : null);
  const before = new Map((prev.data?.results ?? []).map((r) => [r.case_id, r.passed]));
  const d = detail.data;
  return (
    <Modal title={`Ejecución #${id}`} onClose={onClose} wide>
      {detail.error && <ErrorBox error={detail.error} />}
      {!d ? <Loading /> : (
        <div style={{ display: "grid", gap: 10 }}>
          <div className="inline" style={{ gap: 8 }}>
            <Badge tone={RUN_STATUS[d.status][1]}>{RUN_STATUS[d.status][0]}</Badge>
            <span>{d.passed}/{d.total} casos ({d.pass_pct ?? 0} %)</span>
            <span className="muted small">{TRIGGER[d.trigger]} · {fmtDateTime(d.started_at)} · US$ {d.cost_usd.toFixed(4)}</span>
          </div>
          {d.results.map((r) => {
            const was = r.case_id != null ? before.get(r.case_id) : undefined;
            const change = was === undefined || was === r.passed ? null : r.passed ? "Ahora pasa" : "Antes pasaba";
            return (
              <div key={r.id} className="stat" style={{ gap: 4 }}>
                <div className="row">
                  <span className="strong">{r.case_name ?? "(caso eliminado)"}</span>
                  <span className="inline">
                    {change && <Badge tone={r.passed ? "ok" : "bad"}>{change}</Badge>}
                    <Badge tone={r.passed ? "ok" : "bad"}>{r.passed ? "Pasa" : "Falla"}</Badge>
                    {r.latency_ms != null && <span className="muted small">{(r.latency_ms / 1000).toFixed(1)} s</span>}
                  </span>
                </div>
                {r.reply && <div className="small" style={{ whiteSpace: "pre-wrap" }}>🤖 {r.reply}</div>}
                <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {r.checks.map((c, i) => (
                    <li key={i} style={{ color: c.passed ? "var(--ok)" : "var(--bad)" }}>
                      {c.passed ? "✓" : "✗"} {CHECK_LABEL[c.check] ?? c.check}{c.detail ? `: ${c.detail}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
