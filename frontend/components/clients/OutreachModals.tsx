"use client";

// Acciones salientes desde la lista / ficha de clientes: plantilla a una selección e iniciar conversación (§18.3).
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { send, type AgentDetail, type Group, type Template } from "@/lib/api";
import { ErrorBox, Field, Modal, useApi } from "@/components/ui";

type Channel = { id: number; name: string; provider?: string; display_phone?: string | null };
type Options = { bot: "on" | "off"; assign: "me" | "agent" | "group" | "none"; agent_id: string; group_id: string };

function preview(tpl: Template | undefined, values: string[]) {
  if (!tpl) return "";
  let text = tpl.body;
  tpl.variables.forEach((v, i) => {
    const key = tpl.named ? `{{${v}}}` : `{{${i + 1}}}`;
    text = text.split(key).join(values[i] || key);
  });
  return text;
}

function TemplateFields({
  templates,
  name,
  onName,
  values,
  onValues,
  personalize,
}: {
  templates: Template[];
  name: string;
  onName: (n: string) => void;
  values: string[];
  onValues: (v: string[]) => void;
  personalize?: boolean;
}) {
  const tpl = templates.find((t) => `${t.name}|${t.language}` === name);
  return (
    <>
      <Field label="Plantilla aprobada">
        <select
          value={name}
          onChange={(e) => {
            onName(e.target.value);
            const t = templates.find((x) => `${x.name}|${x.language}` === e.target.value);
            onValues(t ? t.variables.map(() => "") : []);
          }}
        >
          <option value="">Elige una plantilla…</option>
          {templates
            .filter((t) => t.status === "APPROVED" && t.supported)
            .map((t) => (
              <option key={`${t.name}|${t.language}`} value={`${t.name}|${t.language}`}>
                {t.name} · {t.language} · {t.category === "MARKETING" ? "Marketing" : t.category === "UTILITY" ? "Utilidad" : t.category}
              </option>
            ))}
        </select>
      </Field>
      {tpl?.variables.map((v, i) => (
        <Field
          key={v + i}
          label={`Variable ${tpl.named ? v : i + 1}`}
          hint={personalize && i === 0 ? "Usa {{nombre}} para el nombre de cada cliente" : undefined}
        >
          <input value={values[i] ?? ""} onChange={(e) => onValues(values.map((x, j) => (j === i ? e.target.value : x)))} />
        </Field>
      ))}
      {tpl && (
        <div className="card" style={{ background: "var(--panel-2)", whiteSpace: "pre-wrap" }}>
          <div className="muted small">Vista previa</div>
          {tpl.header && <strong>{tpl.header}</strong>}
          <div>{preview(tpl, values)}</div>
          {tpl.category === "MARKETING" && (
            <div className="muted small" style={{ marginTop: 6 }}>
              Plantilla de marketing: no se envía a quienes pidieron no recibir publicidad.
            </div>
          )}
        </div>
      )}
    </>
  );
}

function AssignmentFields({ value, onChange }: { value: Options; onChange: (o: Options) => void }) {
  const agents = useApi<AgentDetail[]>("/api/agents");
  const groups = useApi<Group[]>("/api/groups");
  return (
    <>
      <Field label="¿Quién responde cuando el cliente conteste?">
        <select value={value.bot} onChange={(e) => onChange({ ...value, bot: e.target.value as Options["bot"] })}>
          <option value="off">Un asesor (el bot queda en pausa)</option>
          <option value="on">El bot</option>
        </select>
      </Field>
      {value.bot === "off" && (
        <Field label="Asignar a">
          <select value={value.assign} onChange={(e) => onChange({ ...value, assign: e.target.value as Options["assign"] })}>
            <option value="me">A mí</option>
            <option value="agent">Otro asesor</option>
            <option value="group">Cola de un grupo</option>
            <option value="none">Sin asignar</option>
          </select>
        </Field>
      )}
      {value.bot === "off" && value.assign === "agent" && (
        <Field label="Asesor">
          <select value={value.agent_id} onChange={(e) => onChange({ ...value, agent_id: e.target.value })}>
            <option value="">Elige…</option>
            {(agents.data ?? []).filter((a) => a.is_active).map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </Field>
      )}
      {(value.assign === "group" || value.bot === "on") && (
        <Field label="Grupo (opcional)">
          <select value={value.group_id} onChange={(e) => onChange({ ...value, group_id: e.target.value })}>
            <option value="">Sin grupo</option>
            {(groups.data ?? []).map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
          </select>
        </Field>
      )}
    </>
  );
}

const DEFAULT_OPTIONS: Options = { bot: "off", assign: "me", agent_id: "", group_id: "" };

/** Plantilla masiva a los clientes seleccionados (o a todos los del filtro actual). */
export function SendTemplateModal({
  contactIds,
  filter,
  count,
  meId,
  onClose,
  onDone,
}: {
  contactIds: number[] | null;
  filter: Record<string, string | number | boolean> | null;
  count: number;
  meId: number | null;
  onClose: () => void;
  onDone: (r: { id: number; recipients: number }) => void;
}) {
  const templates = useApi<Template[]>("/api/templates");
  const [name, setName] = useState("");
  const [values, setValues] = useState<string[]>([]);
  const [opts, setOpts] = useState<Options>(DEFAULT_OPTIONS);
  const [mode, setMode] = useState<"continue" | "new">("continue");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const [tplName, language] = name.split("|");
    const options: Record<string, unknown> = { bot: opts.bot, conversation: mode };
    if (opts.bot === "off" && opts.assign === "me" && meId) options.assign_agent_id = meId;
    if (opts.bot === "off" && opts.assign === "agent" && opts.agent_id) options.assign_agent_id = Number(opts.agent_id);
    if (opts.group_id) options.assign_group_id = Number(opts.group_id);
    const tagList = tags.split(",").map((t) => t.trim()).filter(Boolean);
    if (tagList.length) options.tags = tagList;
    setBusy(true);
    setError(null);
    try {
      onDone(
        await send<{ id: number; recipients: number }>("/api/campaigns/from-selection", "POST", {
          ...(contactIds ? { contact_ids: contactIds } : { filter: filter ?? {} }),
          template_name: tplName,
          template_language: language,
          params: values,
          options,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Enviar plantilla a ${count.toLocaleString("es")} cliente${count === 1 ? "" : "s"}`}
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !name} onClick={submit}>
            {busy ? "Enviando…" : "Enviar ahora"}
          </button>
        </>
      }
    >
      <ErrorBox error={error ?? templates.error} />
      <TemplateFields templates={templates.data ?? []} name={name} onName={setName} values={values} onValues={setValues} personalize />
      <AssignmentFields value={opts} onChange={setOpts} />
      <Field label="Conversación">
        <select value={mode} onChange={(e) => setMode(e.target.value as "continue" | "new")}>
          <option value="continue">Continuar la última conversación del cliente</option>
          <option value="new">Abrir una conversación nueva</option>
        </select>
      </Field>
      <Field label="Etiquetas (opcional)" hint="Separadas por coma; se agregan a cada conversación">
        <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="seguimiento, octubre" />
      </Field>
      <p className="muted small">
        Se crea una campaña «Selección de clientes» que respeta los bloqueos, el opt-out de marketing y el límite de tu plan.
        Verás el avance en Campañas.
      </p>
    </Modal>
  );
}

/** Iniciar conversación con un cliente: texto dentro de la ventana de 24 h o plantilla fuera de ella. */
export function StartConversationModal({
  contactId,
  contactName,
  onClose,
}: {
  contactId: number;
  contactName: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const channels = useApi<{ channels: Channel[] } | Channel[]>("/api/channels");
  const channelList = useMemo(() => {
    const raw = channels.data;
    const list = Array.isArray(raw) ? raw : raw?.channels ?? [];
    return list.filter((c) => !c.provider || c.provider === "whatsapp_cloud");
  }, [channels.data]);
  const templates = useApi<Template[]>("/api/templates");
  const [channelId, setChannelId] = useState("");
  const [kind, setKind] = useState<"template" | "text">("template");
  const [name, setName] = useState("");
  const [values, setValues] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [opts, setOpts] = useState<Options>(DEFAULT_OPTIONS);
  const [mode, setMode] = useState<"continue" | "new">("continue");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const [tplName, language] = name.split("|");
    setBusy(true);
    setError(null);
    try {
      const r = await send<{ conversation: { id: number } }>("/api/contacts/start-conversation", "POST", {
        contact_id: contactId,
        channel_id: channelId ? Number(channelId) : null,
        ...(kind === "template" ? { template_name: tplName, language, params: values } : { text }),
        bot: opts.bot,
        assign_to_me: opts.bot === "off" && opts.assign === "me",
        agent_id: opts.bot === "off" && opts.assign === "agent" && opts.agent_id ? Number(opts.agent_id) : null,
        group_id: opts.group_id ? Number(opts.group_id) : null,
        mode,
      });
      onClose();
      router.push(`/conversaciones?id=${r.conversation.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Iniciar conversación con ${contactName}`}
      onClose={onClose}
      wide
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button
            className="primary"
            disabled={busy || (kind === "template" ? !name : !text.trim())}
            onClick={submit}
          >
            {busy ? "Enviando…" : "Enviar e ir al chat"}
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      {channelList.length > 1 && (
        <Field label="Número que envía">
          <select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
            <option value="">Principal</option>
            {channelList.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.display_phone ? ` · ${c.display_phone}` : ""}
              </option>
            ))}
          </select>
        </Field>
      )}
      <div className="inline" role="radiogroup" aria-label="Tipo de mensaje">
        <label className="inline small">
          <input type="radio" checked={kind === "template"} onChange={() => setKind("template")} /> Plantilla
        </label>
        <label className="inline small">
          <input type="radio" checked={kind === "text"} onChange={() => setKind("text")} /> Mensaje libre
        </label>
      </div>
      {kind === "template" ? (
        <TemplateFields templates={templates.data ?? []} name={name} onName={setName} values={values} onValues={setValues} />
      ) : (
        <Field label="Mensaje" hint="Solo si el cliente te escribió en las últimas 24 h; si no, WhatsApp exige plantilla.">
          <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
      )}
      <AssignmentFields value={opts} onChange={setOpts} />
      <Field label="Conversación">
        <select value={mode} onChange={(e) => setMode(e.target.value as "continue" | "new")}>
          <option value="continue">Continuar la última conversación</option>
          <option value="new">Abrir una conversación nueva</option>
        </select>
      </Field>
    </Modal>
  );
}
