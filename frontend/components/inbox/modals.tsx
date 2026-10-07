"use client";

import { useMemo, useState } from "react";
import {
  api,
  send,
  type AgentDetail,
  type Conversation,
  type Group,
  type Message,
  type Resource,
  type Settings,
  type Template,
} from "@/lib/api";
import { Badge, Empty, ErrorBox, Field, Loading, Modal, useAction, useApi } from "@/components/ui";

export function TransferModal({
  conversation,
  onClose,
  onDone,
}: {
  conversation: Conversation;
  onClose: () => void;
  onDone: (c: Conversation) => void;
}) {
  const agents = useApi<AgentDetail[]>("/api/agents");
  const groups = useApi<Group[]>("/api/groups");
  const [agentId, setAgentId] = useState<string>(conversation.assigned_agent?.id.toString() ?? "");
  const [groupId, setGroupId] = useState<string>(conversation.group?.id.toString() ?? "");
  const [note, setNote] = useState("");
  const [run, busy, error] = useAction();

  async function submit() {
    const c = await run(() =>
      send<Conversation>(`/api/conversations/${conversation.id}/transfer`, "POST", {
        agent_id: agentId ? Number(agentId) : null,
        group_id: groupId ? Number(groupId) : 0,
        note: note.trim() || null,
      }),
    );
    if (c) onDone(c);
  }

  return (
    <Modal
      title="Transferir conversación"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || (!agentId && !groupId)} onClick={submit}>
            Transferir
          </button>
        </>
      }
    >
      <Field label="Asesor" hint="Vacío = queda sin asignar en el grupo">
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">Sin asignar</option>
          {(agents.data ?? [])
            .filter((a) => a.is_active)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} {a.online ? "● conectado" : ""}
              </option>
            ))}
        </select>
      </Field>
      <Field label="Grupo">
        <select value={groupId} onChange={(e) => setGroupId(e.target.value)}>
          <option value="">Sin grupo</option>
          {(groups.data ?? []).map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Nota para el asesor (opcional)">
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <ErrorBox error={error} />
    </Modal>
  );
}

export function CloseModal({
  conversation,
  onClose,
  onDone,
}: {
  conversation: Conversation;
  onClose: () => void;
  onDone: (c: Conversation) => void;
}) {
  const cfg = useApi<Settings["conversations"]>("/api/settings/conversations");
  // Preselección: la ya fijada en la conversación, o la sugerida por la IA.
  const aiSuggestion = conversation.ai_suggestions?.typification;
  const [typification, setTypification] = useState(conversation.typification ?? aiSuggestion?.value ?? "");
  const [run, busy, error] = useAction();
  const required = cfg.data?.require_typification ?? true;

  async function submit() {
    const c = await run(() =>
      send<Conversation>(`/api/conversations/${conversation.id}/close`, "POST", { typification: typification || null }),
    );
    if (c) onDone(c);
  }

  return (
    <Modal
      title="Cerrar conversación"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || (required && !typification)} onClick={submit}>
            Cerrar conversación
          </button>
        </>
      }
    >
      {!cfg.data ? (
        <Loading />
      ) : (
        <Field
          label={`Tipificación${required ? "" : " (opcional)"}`}
          hint={
            aiSuggestion && typification === aiSuggestion.value && !conversation.typification
              ? `✨ Sugerida por IA (${Math.round(aiSuggestion.confidence * 100)} %). Revísala antes de cerrar.`
              : "Resultado de la conversación, para los reportes"
          }
        >
          <select value={typification} onChange={(e) => setTypification(e.target.value)} autoFocus>
            <option value="">Selecciona…</option>
            {cfg.data.typifications.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
      )}
      <ErrorBox error={error} />
    </Modal>
  );
}

function render(tpl: Template, values: string[]) {
  let body = tpl.body;
  tpl.variables.forEach((v, i) => {
    body = body.replace(new RegExp(`\\{\\{\\s*${v}\\s*\\}\\}`, "g"), values[i] || `{{${v}}}`);
  });
  return tpl.header ? `*${tpl.header}*\n${body}` : body;
}

export function TemplateModal({
  conversation,
  onClose,
  onSent,
}: {
  conversation: Conversation;
  onClose: () => void;
  onSent: (m: Message) => void;
}) {
  const templates = useApi<Template[]>("/api/templates");
  const [key, setKey] = useState("");
  const [values, setValues] = useState<string[]>([]);
  const [run, busy, error] = useAction();

  const usable = useMemo(
    () => (templates.data ?? []).filter((t) => t.status === "APPROVED"),
    [templates.data],
  );
  const tpl = usable.find((t) => `${t.name}|${t.language}` === key) ?? null;
  const blockedMarketing = tpl?.category === "MARKETING" && conversation.contact.marketing_opt_out;

  function pick(k: string) {
    setKey(k);
    const t = usable.find((x) => `${x.name}|${x.language}` === k);
    setValues(t ? t.variables.map((v) => (v === "1" || v === "nombre" ? (conversation.contact.name ?? "").split(" ")[0] : "")) : []);
  }

  async function submit() {
    if (!tpl) return;
    const m = await run(() =>
      send<Message>(`/api/conversations/${conversation.id}/template`, "POST", {
        name: tpl.name,
        language: tpl.language,
        values,
      }),
    );
    if (m) onSent(m);
  }

  return (
    <Modal
      title="Enviar plantilla"
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button
            className="primary"
            disabled={busy || !tpl || !tpl.supported || blockedMarketing || values.some((v) => !v.trim())}
            onClick={submit}
          >
            Enviar plantilla
          </button>
        </>
      }
    >
      {templates.error && <ErrorBox error={templates.error} />}
      {!templates.data && !templates.error ? (
        <Loading />
      ) : usable.length === 0 ? (
        <Empty>No hay plantillas aprobadas. Revisa WA_WABA_ID y tus plantillas en Meta.</Empty>
      ) : (
        <>
          <Field label="Plantilla">
            <select value={key} onChange={(e) => pick(e.target.value)} autoFocus>
              <option value="">Selecciona…</option>
              {usable.map((t) => (
                <option key={`${t.name}|${t.language}`} value={`${t.name}|${t.language}`} disabled={!t.supported}>
                  {t.name} ({t.language}) · {t.category}
                  {t.supported ? "" : " — no soportada"}
                </option>
              ))}
            </select>
          </Field>
          {tpl && (
            <div className="grid2">
              <div className="stack">
                {!tpl.supported && <ErrorBox error={tpl.unsupported_reason} />}
                {blockedMarketing && (
                  <ErrorBox error="Este contacto pidió no recibir marketing. Usa una plantilla de utilidad." />
                )}
                {tpl.variables.length === 0 && <p className="muted small">Esta plantilla no tiene variables.</p>}
                {tpl.variables.map((v, i) => (
                  <Field key={v} label={`Variable {{${v}}}`}>
                    <input
                      value={values[i] ?? ""}
                      onChange={(e) => setValues(values.map((x, j) => (j === i ? e.target.value : x)))}
                    />
                  </Field>
                ))}
              </div>
              <div>
                <div className="small muted" style={{ marginBottom: 4 }}>
                  Vista previa <Badge tone="neutral">{tpl.category}</Badge>
                </div>
                <div className="bubble out campaign" style={{ maxWidth: "100%" }}>
                  <p>{render(tpl, values)}</p>
                </div>
              </div>
            </div>
          )}
        </>
      )}
      <ErrorBox error={error} />
    </Modal>
  );
}

function size(n: number) {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`;
}

export function ResourceModal({
  conversation,
  onClose,
  onSent,
}: {
  conversation: Conversation;
  onClose: () => void;
  onSent: (m: Message) => void;
}) {
  const resources = useApi<Resource[]>("/api/resources");
  const [q, setQ] = useState("");
  const [caption, setCaption] = useState("");
  const [run, busy, error] = useAction();
  const list = (resources.data ?? []).filter((r) => r.name.toLowerCase().includes(q.toLowerCase()));

  async function sendRes(id: number) {
    const m = await run(() =>
      api<Message>(`/api/conversations/${conversation.id}/resource`, {
        method: "POST",
        body: JSON.stringify({ resource_id: id, caption: caption.trim() || null }),
      }),
    );
    if (m) onSent(m);
  }

  return (
    <Modal title="Enviar recurso" onClose={onClose} wide>
      <div className="grid2">
        <input placeholder="Buscar recurso" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <input placeholder="Texto que acompaña (opcional)" value={caption} onChange={(e) => setCaption(e.target.value)} />
      </div>
      {resources.error && <ErrorBox error={resources.error} />}
      {!resources.data && !resources.error ? (
        <Loading />
      ) : list.length === 0 ? (
        <Empty>No hay recursos. Súbelos en Configuraciones → Gestor de recursos.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <tbody>
              {list.map((r) => (
                <tr key={r.id}>
                  <td>
                    <div className="strong">{r.name}</div>
                    <div className="muted small">
                      {r.mime} · {size(r.size)}
                    </div>
                  </td>
                  <td className="right">
                    <button className="primary" disabled={busy} onClick={() => sendRes(r.id)}>
                      Enviar
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorBox error={error} />
    </Modal>
  );
}
