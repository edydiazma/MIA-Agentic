"use client";

import { useEffect, useRef, useState } from "react";
import { api, fmtDateTime, qs, send } from "@/lib/api";
import type { AIAgent, AIConnection } from "@/lib/ai-types";
import {
  DOC_STATUS,
  SOURCE_HINT,
  SOURCE_LABEL,
  type KChunk,
  type KDocument,
  type KGap,
  type KSearchResult,
  type KSettings,
  type KSource,
  type KSourceType,
  type KStats,
} from "@/lib/knowledge-types";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageHeader,
  Stat,
  Tabs,
  Toggle,
  useAction,
  useApi,
} from "@/components/ui";
import { AdminNotice, useIsAdmin } from "@/components/config/common";

type Tab = "sources" | "documents" | "playground" | "gaps" | "usage";
const patch = <T,>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const NEW_TYPES: KSourceType[] = ["upload", "website", "catalog", "conversations", "faq", "api"];
const SRC_STATUS: Record<KSource["status"], [string, "ok" | "bad" | "warn" | "neutral" | "info"]> = {
  idle: ["Sin sincronizar", "neutral"],
  syncing: ["Sincronizando…", "info"],
  ready: ["Al día", "ok"],
  error: ["Error", "bad"],
};

export default function KnowledgePage() {
  const [tab, setTab] = useState<Tab>("sources");
  const [docSource, setDocSource] = useState<number | null>(null);
  return (
    <>
      <PageHeader
        title="Base de conocimiento"
        subtitle="Fuentes que el agente de IA, el copiloto y el asistente consultan con búsqueda semántica y citan al responder."
      />
      <AdminNotice />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          ["sources", "Fuentes"],
          ["documents", "Documentos"],
          ["playground", "Probar búsqueda"],
          ["gaps", "Vacíos"],
          ["usage", "Uso y ajustes"],
        ]}
      />
      {tab === "sources" && (
        <SourcesTab
          onOpen={(id) => {
            setDocSource(id);
            setTab("documents");
          }}
        />
      )}
      {tab === "documents" && <DocumentsTab sourceId={docSource} onSource={setDocSource} />}
      {tab === "playground" && <PlaygroundTab />}
      {tab === "gaps" && <GapsTab />}
      {tab === "usage" && <UsageTab />}
    </>
  );
}

// --- Fuentes ---------------------------------------------------------------------------------------------------
function SourcesTab({ onOpen }: { onOpen: (id: number) => void }) {
  const isAdmin = useIsAdmin();
  const sources = useApi<KSource[]>("/api/knowledge-base/sources");
  const agents = useApi<AIAgent[]>("/api/bots");
  const [editing, setEditing] = useState<KSource | "new" | null>(null);
  const [run, busy, error] = useAction();
  const upload = useRef<HTMLInputElement>(null);
  const [uploadTo, setUploadTo] = useState<number | null>(null);

  // mientras alguna fuente sincroniza, refresca cada 4 s
  const syncing = (sources.data ?? []).some((s) => s.status === "syncing");
  useEffect(() => {
    if (!syncing) return;
    const t = setInterval(() => sources.reload(), 4000);
    return () => clearInterval(t);
  }, [syncing, sources]);

  const sync = (s: KSource, force = false) =>
    run(async () => {
      await send(`/api/knowledge-base/sources/${s.id}/sync${force ? "?force=true" : ""}`, "POST");
      sources.reload();
    });
  const uploadFiles = (files: FileList) =>
    run(async () => {
      const fd = new FormData();
      Array.from(files).forEach((f) => fd.append("files", f));
      await api(`/api/knowledge-base/sources/${uploadTo}/upload`, { method: "POST", body: fd });
      sources.reload();
    });

  if (sources.loading && !sources.data) return <Loading />;
  const agentName = (id: number) => agents.data?.find((a) => a.id === id)?.name ?? `#${id}`;
  return (
    <Card
      title="Fuentes"
      actions={isAdmin ? <button className="primary" onClick={() => setEditing("new")}>+ Nueva fuente</button> : undefined}
    >
      <ErrorBox error={sources.error || error} />
      <input
        ref={upload}
        type="file"
        hidden
        multiple
        accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.html,.htm,.json"
        onChange={(e) => {
          if (e.target.files?.length) uploadFiles(e.target.files);
          e.target.value = "";
        }}
      />
      {(sources.data ?? []).length === 0 ? (
        <Empty>Aún no hay fuentes. Sube archivos, conecta tu sitio web o el catálogo.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Fuente</th>
              <th>Estado</th>
              <th>Documentos</th>
              <th>Agentes</th>
              <th>Última sincronización</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(sources.data ?? []).map((s) => (
              <tr key={s.id} className={s.is_active ? "" : "muted"}>
                <td>
                  <button className="link" onClick={() => onOpen(s.id)}>
                    {s.name}
                  </button>
                  <div className="muted small">
                    {SOURCE_LABEL[s.type]}
                    {s.refresh_hours ? ` · cada ${s.refresh_hours} h` : ""}
                    {!s.is_active && " · desactivada"}
                  </div>
                </td>
                <td>
                  <Badge tone={SRC_STATUS[s.status][1]}>{SRC_STATUS[s.status][0]}</Badge>
                  {s.last_error && <div className="error small">{s.last_error}</div>}
                </td>
                <td>
                  {s.documents_count.toLocaleString("es")}
                  <div className="muted small">{s.chunks_count.toLocaleString("es")} fragmentos</div>
                </td>
                <td className="small">{s.ai_agent_ids.length ? s.ai_agent_ids.map(agentName).join(", ") : "Todos"}</td>
                <td className="small">{s.last_synced_at ? fmtDateTime(s.last_synced_at) : "—"}</td>
                <td className="row-actions">
                  {isAdmin && s.type === "upload" && (
                    <button
                      disabled={busy}
                      onClick={() => {
                        setUploadTo(s.id);
                        upload.current?.click();
                      }}
                    >
                      ⬆ Subir
                    </button>
                  )}
                  {isAdmin && s.type !== "api" && (
                    <button disabled={busy || s.status === "syncing"} onClick={() => sync(s)}>
                      Sincronizar
                    </button>
                  )}
                  {isAdmin && <button onClick={() => setEditing(s)}>Editar</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && (
        <SourceModal
          source={editing === "new" ? null : editing}
          agents={agents.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            sources.reload();
          }}
        />
      )}
    </Card>
  );
}

function SourceModal({
  source,
  agents,
  onClose,
  onSaved,
}: {
  source: KSource | null;
  agents: AIAgent[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const cfg = (source?.config ?? {}) as Record<string, unknown>;
  const [type, setType] = useState<KSourceType>(source?.type ?? "upload");
  const [name, setName] = useState(source?.name ?? "");
  const [url, setUrl] = useState(String(cfg.url ?? ""));
  const [maxPages, setMaxPages] = useState(Number(cfg.max_pages ?? 30));
  const [exclude, setExclude] = useState(((cfg.exclude as string[]) ?? []).join(", "));
  const [typifs, setTypifs] = useState(((cfg.typifications as string[]) ?? []).join(", "));
  const [minQa, setMinQa] = useState(String(cfg.min_qa_score ?? ""));
  const [faq, setFaq] = useState(
    ((cfg.items as { question: string; answer: string }[]) ?? []).map((i) => `${i.question}\n${i.answer}`).join("\n\n"),
  );
  const [refresh, setRefresh] = useState(source?.refresh_hours ?? (type === "website" ? 24 : 0));
  const [agentIds, setAgentIds] = useState<number[]>(source?.ai_agent_ids ?? []);
  const [active, setActive] = useState(source?.is_active ?? true);
  const [run, busy, error] = useAction();

  const config = (): Record<string, unknown> => {
    const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
    if (type === "website") return { url, max_pages: maxPages, exclude: split(exclude) };
    if (type === "conversations")
      return { typifications: split(typifs), ...(minQa ? { min_qa_score: Number(minQa) } : {}) };
    if (type === "faq")
      return {
        items: faq
          .split(/\n\s*\n/)
          .map((b) => b.trim())
          .filter(Boolean)
          .map((b) => {
            const [q, ...a] = b.split("\n");
            return { question: q.trim(), answer: a.join("\n").trim() };
          }),
      };
    return {};
  };
  const save = () =>
    run(async () => {
      const body = { name, config: config(), refresh_hours: refresh || null, ai_agent_ids: agentIds, is_active: active };
      if (source) await patch(`/api/knowledge-base/sources/${source.id}`, { ...body, refresh_hours: refresh || 0 });
      else await send("/api/knowledge-base/sources", "POST", { ...body, type });
      onSaved();
    });
  const remove = () =>
    run(async () => {
      if (!source || !confirm(`¿Eliminar la fuente «${source.name}» y todos sus documentos?`)) return;
      await send(`/api/knowledge-base/sources/${source.id}`, "DELETE");
      onSaved();
    });

  return (
    <Modal
      title={source ? `Editar ${source.name}` : "Nueva fuente"}
      onClose={onClose}
      wide
      footer={
        <>
          {source && source.type !== "legacy_docs" && (
            <button className="danger" onClick={remove} disabled={busy}>
              Eliminar
            </button>
          )}
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" onClick={save} disabled={busy || !name.trim()}>
            Guardar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      {!source && (
        <Field label="Tipo" hint={SOURCE_HINT[type]}>
          <select value={type} onChange={(e) => setType(e.target.value as KSourceType)}>
            {NEW_TYPES.map((t) => (
              <option key={t} value={t}>
                {SOURCE_LABEL[t]}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label="Nombre">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ej. Políticas comerciales" />
      </Field>
      {type === "website" && (
        <>
          <Field label="URL del sitio">
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.tuempresa.com" />
          </Field>
          <Field label="Máximo de páginas" hint="Hasta 200.">
            <input type="number" min={1} max={200} value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))} />
          </Field>
          <Field label="Excluir rutas que contengan" hint="Separadas por coma, ej. /blog, /carrito">
            <input value={exclude} onChange={(e) => setExclude(e.target.value)} />
          </Field>
        </>
      )}
      {type === "conversations" && (
        <>
          <Field label="Tipificaciones" hint="Vacío = las marcadas como éxito.">
            <input value={typifs} onChange={(e) => setTypifs(e.target.value)} placeholder="Venta, Cita agendada" />
          </Field>
          <Field label="Puntaje mínimo de calidad" hint="Vacío = el de Uso y ajustes (80).">
            <input type="number" min={0} max={100} value={minQa} onChange={(e) => setMinQa(e.target.value)} />
          </Field>
        </>
      )}
      {type === "faq" && (
        <Field label="Preguntas y respuestas" hint="Primera línea: la pregunta; siguientes: la respuesta. Separa cada par con una línea en blanco.">
          <textarea rows={10} value={faq} onChange={(e) => setFaq(e.target.value)} />
        </Field>
      )}
      {type !== "upload" && type !== "api" && type !== "faq" && type !== "legacy_docs" && (
        <Field label="Actualizar cada (horas)" hint="0 = solo manual.">
          <input type="number" min={0} max={720} value={refresh} onChange={(e) => setRefresh(Number(e.target.value))} />
        </Field>
      )}
      <Field label="Disponible para" hint="Ningún agente marcado = todos los agentes de IA.">
        <div className="stack" style={{ gap: 4 }}>
          {agents.map((a) => (
            <label key={a.id} className="inline">
              <input
                type="checkbox"
                checked={agentIds.includes(a.id)}
                onChange={() => setAgentIds((ids) => (ids.includes(a.id) ? ids.filter((x) => x !== a.id) : [...ids, a.id]))}
              />
              <span>{a.name}</span>
            </label>
          ))}
        </div>
      </Field>
      <Toggle checked={active} onChange={setActive} label="Fuente activa" />
    </Modal>
  );
}

// --- Documentos ------------------------------------------------------------------------------------------------
function DocumentsTab({ sourceId, onSource }: { sourceId: number | null; onSource: (id: number | null) => void }) {
  const isAdmin = useIsAdmin();
  const sources = useApi<KSource[]>("/api/knowledge-base/sources");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const docs = useApi<{ total: number; items: KDocument[] }>(
    `/api/knowledge-base/documents${qs({ source_id: sourceId, status, q, limit: 50, offset })}`,
  );
  const [open, setOpen] = useState<KDocument | null>(null);
  const [run, busy, error] = useAction();
  const srcName = (id: number) => sources.data?.find((s) => s.id === id)?.name ?? `#${id}`;
  const act = (fn: () => Promise<unknown>) =>
    run(async () => {
      await fn();
      docs.reload();
    });

  return (
    <Card
      title="Documentos"
      actions={
        <div className="inline">
          <select value={sourceId ?? ""} onChange={(e) => onSource(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Todas las fuentes</option>
            {(sources.data ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Todos los estados</option>
            {Object.entries(DOC_STATUS).map(([k, [label]]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
          <input placeholder="Buscar título…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      }
    >
      <ErrorBox error={docs.error || error} />
      {docs.loading && !docs.data ? (
        <Loading />
      ) : (docs.data?.items ?? []).length === 0 ? (
        <Empty>No hay documentos con estos filtros.</Empty>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Documento</th>
                <th>Estado</th>
                <th>Fragmentos</th>
                <th>Vigencia</th>
                <th>Actualizado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(docs.data?.items ?? []).map((d) => {
                const expired = d.valid_until && new Date(d.valid_until) < new Date();
                return (
                  <tr key={d.id}>
                    <td>
                      <button className="link" onClick={() => setOpen(d)}>
                        {d.title}
                      </button>
                      <div className="muted small">
                        {srcName(d.source_id)}
                        {d.uri && d.uri.startsWith("http") ? ` · ${d.uri}` : ""}
                        {d.language ? ` · ${d.language}` : ""}
                      </div>
                    </td>
                    <td>
                      <Badge tone={DOC_STATUS[d.status][1]}>{DOC_STATUS[d.status][0]}</Badge>
                      {d.error && <div className="error small">{d.error}</div>}
                    </td>
                    <td>
                      {d.chunks_count}
                      {d.tokens != null && <div className="muted small">{d.tokens.toLocaleString("es")} tokens</div>}
                    </td>
                    <td className="small">
                      {d.valid_until ? (
                        <Badge tone={expired ? "warn" : "neutral"}>{expired ? "Vencido" : `Hasta ${fmtDateTime(d.valid_until)}`}</Badge>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="small">{fmtDateTime(d.updated_at)}</td>
                    <td className="row-actions">
                      {isAdmin && d.status !== "excluded" && (
                        <button disabled={busy} onClick={() => act(() => patch(`/api/knowledge-base/documents/${d.id}`, { excluded: true }))}>
                          Excluir
                        </button>
                      )}
                      {isAdmin && d.status === "excluded" && (
                        <button disabled={busy} onClick={() => act(() => patch(`/api/knowledge-base/documents/${d.id}`, { excluded: false }))}>
                          Incluir
                        </button>
                      )}
                      {isAdmin && (
                        <button disabled={busy} onClick={() => act(() => send(`/api/knowledge-base/documents/${d.id}/reindex`, "POST"))}>
                          Reindexar
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="inline" style={{ justifyContent: "space-between", marginTop: 8 }}>
            <span className="muted small">{docs.data?.total.toLocaleString("es")} documentos</span>
            <div className="inline">
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>
                ← Anteriores
              </button>
              <button disabled={offset + 50 >= (docs.data?.total ?? 0)} onClick={() => setOffset(offset + 50)}>
                Siguientes →
              </button>
            </div>
          </div>
        </>
      )}
      {open && <DocumentModal doc={open} canEdit={isAdmin} onClose={() => setOpen(null)} onSaved={docs.reload} />}
    </Card>
  );
}

function DocumentModal({ doc, canEdit, onClose, onSaved }: { doc: KDocument; canEdit: boolean; onClose: () => void; onSaved: () => void }) {
  const chunks = useApi<KChunk[]>(`/api/knowledge-base/documents/${doc.id}/chunks`);
  const [validUntil, setValidUntil] = useState(doc.valid_until ? doc.valid_until.slice(0, 10) : "");
  const [run, busy, error] = useAction();
  const saveValidity = () =>
    run(async () => {
      await patch(
        `/api/knowledge-base/documents/${doc.id}`,
        validUntil ? { valid_until: new Date(`${validUntil}T23:59:59`).toISOString() } : { clear_valid_until: true },
      );
      onSaved();
    });
  return (
    <Modal title={doc.title} onClose={onClose} wide>
      <ErrorBox error={chunks.error || error} />
      {doc.uri && <p className="muted small">{doc.uri}</p>}
      <div className="inline" style={{ alignItems: "flex-end" }}>
        <Field label="Vigente hasta" hint="Promociones o precios: después de esta fecha no se usa.">
          <input type="date" value={validUntil} disabled={!canEdit} onChange={(e) => setValidUntil(e.target.value)} />
        </Field>
        {canEdit && (
          <button onClick={saveValidity} disabled={busy}>
            Guardar vigencia
          </button>
        )}
      </div>
      <h3>Fragmentos ({chunks.data?.length ?? "…"})</h3>
      {chunks.loading && !chunks.data ? (
        <Loading />
      ) : (
        <div className="stack" style={{ gap: 8, maxHeight: 420, overflow: "auto" }}>
          {(chunks.data ?? []).map((c) => (
            <div key={c.id} className="card" style={{ padding: 10 }}>
              <div className="muted small">
                #{c.ordinal + 1}
                {c.heading ? ` · ${c.heading}` : ""} · {c.tokens ?? "?"} tokens ·{" "}
                {c.embedded ? `embedding ${c.embedding_model}` : "solo texto (sin embedding)"}
              </div>
              <div className="small" style={{ whiteSpace: "pre-wrap" }}>
                {c.content}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

// --- Probar búsqueda -------------------------------------------------------------------------------------------
function PlaygroundTab() {
  const agents = useApi<AIAgent[]>("/api/bots");
  const [query, setQuery] = useState("");
  const [agentId, setAgentId] = useState<number | "">("");
  const [result, setResult] = useState<KSearchResult | null>(null);
  const [showPrompt, setShowPrompt] = useState(false);
  const [run, busy, error] = useAction();
  const search = () =>
    run(async () =>
      setResult(
        await send<KSearchResult>("/api/knowledge-base/search", "POST", { query, ai_agent_id: agentId || null, limit: 6 }),
      ),
    );
  return (
    <Card title="Probar búsqueda">
      <p className="muted small">Escribe una pregunta como la haría un cliente y mira qué fragmentos recibiría el agente.</p>
      <div className="inline">
        <input
          style={{ flex: 1 }}
          value={query}
          placeholder="¿Cuánto dura la garantía?"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && query.trim() && search()}
        />
        <select value={agentId} onChange={(e) => setAgentId(e.target.value ? Number(e.target.value) : "")}>
          <option value="">Cualquier agente</option>
          {(agents.data ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <button className="primary" onClick={search} disabled={busy || !query.trim()}>
          Buscar
        </button>
      </div>
      <ErrorBox error={error} />
      {result && (
        <div className="stack" style={{ gap: 8, marginTop: 12 }}>
          <div className="muted small">
            {result.passages.length} fragmentos · {result.latency_ms} ms ·{" "}
            {result.semantic ? "búsqueda semántica + texto" : "solo texto (configura un proveedor de embeddings)"}
          </div>
          {result.passages.length === 0 && <Empty>Sin resultados: el agente diría «no tengo esa información».</Empty>}
          {result.passages.map((p) => (
            <div key={p.chunk_id} className="card" style={{ padding: 10 }}>
              <div className="inline" style={{ justifyContent: "space-between" }}>
                <strong>
                  [{p.label}] {p.title}
                  {p.heading && p.heading !== p.title ? ` › ${p.heading}` : ""}
                </strong>
                <span className="muted small">
                  {p.source ? SOURCE_LABEL[p.source as KSourceType] : ""} · puntaje {p.score.toFixed(4)}
                </span>
              </div>
              {p.uri && p.uri.startsWith("http") && (
                <a className="small" href={p.uri} target="_blank" rel="noreferrer">
                  {p.uri}
                </a>
              )}
              <div className="small" style={{ whiteSpace: "pre-wrap" }}>
                {p.content}
              </div>
            </div>
          ))}
          {result.prompt && (
            <button className="link" onClick={() => setShowPrompt(!showPrompt)}>
              {showPrompt ? "Ocultar" : "Ver"} lo que recibe el modelo
            </button>
          )}
          {showPrompt && <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{result.prompt}</pre>}
        </div>
      )}
    </Card>
  );
}

// --- Vacíos ----------------------------------------------------------------------------------------------------
function GapsTab() {
  const isAdmin = useIsAdmin();
  const [status, setStatus] = useState<KGap["status"]>("open");
  const gaps = useApi<KGap[]>(`/api/knowledge-base/gaps?status=${status}`);
  const [answering, setAnswering] = useState<KGap | null>(null);
  const [answer, setAnswer] = useState("");
  const [question, setQuestion] = useState("");
  const [run, busy, error] = useAction();
  const ignore = (g: KGap) =>
    run(async () => {
      await send(`/api/knowledge-base/gaps/${g.id}/ignore`, "POST");
      gaps.reload();
    });
  const save = () =>
    run(async () => {
      if (!answering) return;
      await send(`/api/knowledge-base/gaps/${answering.id}/answer`, "POST", { answer, question });
      setAnswering(null);
      gaps.reload();
    });
  return (
    <Card
      title="Vacíos de conocimiento"
      actions={
        <select value={status} onChange={(e) => setStatus(e.target.value as KGap["status"])}>
          <option value="open">Abiertos</option>
          <option value="answered">Respondidos</option>
          <option value="ignored">Ignorados</option>
        </select>
      }
    >
      <p className="muted small">
        Preguntas de clientes que la base no pudo responder, agrupadas por similitud. «Responder» crea una pregunta frecuente que el
        agente usa desde ese momento.
      </p>
      <ErrorBox error={gaps.error || error} />
      {gaps.loading && !gaps.data ? (
        <Loading />
      ) : (gaps.data ?? []).length === 0 ? (
        <Empty>No hay vacíos {status === "open" ? "abiertos" : ""}. 🎉</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Pregunta</th>
              <th>Veces</th>
              <th>Última vez</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(gaps.data ?? []).map((g) => (
              <tr key={g.id}>
                <td>
                  <strong>{g.topic}</strong>
                  {g.examples.length > 1 && (
                    <ul className="muted small">
                      {g.examples.slice(1, 4).map((e, i) => (
                        <li key={i}>{e}</li>
                      ))}
                    </ul>
                  )}
                </td>
                <td>{g.occurrences}</td>
                <td className="small">{fmtDateTime(g.last_seen_at)}</td>
                <td className="row-actions">
                  {isAdmin && g.status === "open" && (
                    <>
                      <button
                        className="primary"
                        onClick={() => {
                          setAnswering(g);
                          setQuestion(g.topic);
                          setAnswer("");
                        }}
                      >
                        Responder
                      </button>
                      <button disabled={busy} onClick={() => ignore(g)}>
                        Ignorar
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {answering && (
        <Modal
          title="Responder pregunta"
          onClose={() => setAnswering(null)}
          footer={
            <>
              <button onClick={() => setAnswering(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !answer.trim()} onClick={save}>
                Guardar en preguntas frecuentes
              </button>
            </>
          }
        >
          <Field label="Pregunta">
            <input value={question} onChange={(e) => setQuestion(e.target.value)} />
          </Field>
          <Field label="Respuesta" hint="El agente la usará y citará como fuente.">
            <textarea rows={6} value={answer} onChange={(e) => setAnswer(e.target.value)} />
          </Field>
        </Modal>
      )}
    </Card>
  );
}

// --- Uso y ajustes ---------------------------------------------------------------------------------------------
function UsageTab() {
  const isAdmin = useIsAdmin();
  const stats = useApi<KStats>("/api/knowledge-base/stats?days=30");
  const settings = useApi<KSettings>("/api/settings/knowledge");
  const connections = useApi<AIConnection[]>(isAdmin ? "/api/ai/connections" : null);
  const [form, setForm] = useState<KSettings | null>(null);
  const [run, busy, error] = useAction();
  useEffect(() => {
    if (settings.data) setForm(settings.data);
  }, [settings.data]);
  const s = stats.data;
  const embedConns = (connections.data ?? []).filter((c) => ["voyage", "openai", "azure_openai", "openai_compatible"].includes(c.provider));
  const set = <K extends keyof KSettings>(k: K, v: KSettings[K]) => setForm((f) => (f ? { ...f, [k]: v } : f));
  const save = () =>
    run(async () => {
      await send("/api/settings/knowledge", "PUT", form);
      settings.reload();
    });

  return (
    <>
      <Card title="Uso (últimos 30 días)">
        <ErrorBox error={stats.error} />
        {s && (
          <div className="stats-grid">
            <Stat label="Consultas" value={s.queries.toLocaleString("es")} />
            <Stat
              label="Respondidas con fuentes"
              value={s.answer_rate == null ? "—" : `${Math.round(s.answer_rate * 100)}%`}
              hint={`${s.answered} sí · ${s.unanswered} no`}
              tone={s.answer_rate != null && s.answer_rate < 0.6 ? "warn" : undefined}
            />
            <Stat label="Vacíos abiertos" value={s.open_gaps} tone={s.open_gaps ? "warn" : undefined} />
            <Stat label="Documentos" value={s.documents.toLocaleString("es")} hint={`${s.chunks.toLocaleString("es")} fragmentos`} />
            <Stat label="Con error" value={s.failed_documents} tone={s.failed_documents ? "bad" : undefined} />
            <Stat label="Latencia promedio" value={s.avg_latency_ms == null ? "—" : `${s.avg_latency_ms} ms`} />
            <Stat
              label="Costo de embeddings"
              value={`US$ ${s.embedding_cost_usd.toLocaleString("es", { maximumFractionDigits: 4 })}`}
              hint={`${s.embedding_tokens.toLocaleString("es")} tokens`}
            />
          </div>
        )}
      </Card>
      {form && (
        <Card title="Ajustes" actions={isAdmin ? <button className="primary" onClick={save} disabled={busy}>Guardar</button> : undefined}>
          <ErrorBox error={settings.error || error} />
          <Toggle checked={form.enabled} onChange={(v) => set("enabled", v)} label="Usar la base de conocimiento con RAG" />
          <Field
            label="Conexión de embeddings"
            hint="Voyage AI (voyage-3.5) u OpenAI (text-embedding-3-small). Sin conexión se usa la clave del servidor; sin ninguna, solo búsqueda por texto. Cambiarla requiere reindexar."
          >
            <select
              value={form.embedding_connection_id ?? ""}
              disabled={!isAdmin}
              onChange={(e) => set("embedding_connection_id", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Automática (clave del servidor)</option>
              {embedConns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} — {c.model}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Conexión de rerank (opcional)" hint="Voyage rerank-2.5: reordena los resultados para más precisión.">
            <select
              value={form.rerank_connection_id ?? ""}
              disabled={!isAdmin}
              onChange={(e) => set("rerank_connection_id", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Sin rerank</option>
              {embedConns.filter((c) => c.provider === "voyage").map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} — {c.model}
                </option>
              ))}
            </select>
          </Field>
          <div className="inline">
            <Field label="Fragmentos por respuesta">
              <input type="number" min={1} max={20} value={form.top_k} onChange={(e) => set("top_k", Number(e.target.value))} />
            </Field>
            <Field label="Máx. caracteres en el prompt">
              <input
                type="number"
                min={1000}
                max={40000}
                step={500}
                value={form.max_context_chars}
                onChange={(e) => set("max_context_chars", Number(e.target.value))}
              />
            </Field>
            <Field label="Calidad mínima de conversaciones">
              <input
                type="number"
                min={0}
                max={100}
                value={form.conversations_min_qa_score}
                onChange={(e) => set("conversations_min_qa_score", Number(e.target.value))}
              />
            </Field>
            <Field label="Similitud para agrupar vacíos" hint="0.85 recomendado">
              <input
                type="number"
                min={0.5}
                max={0.99}
                step={0.01}
                value={form.gap_similarity}
                onChange={(e) => set("gap_similarity", Number(e.target.value))}
              />
            </Field>
          </div>
        </Card>
      )}
    </>
  );
}
