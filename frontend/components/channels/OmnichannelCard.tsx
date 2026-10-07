"use client";

import { useState } from "react";
import { CHANNEL_ICONS, CHANNEL_LABELS, api, send, type Bot, type Channel, type ChannelProvider } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Modal, Toggle, useAction } from "@/components/ui";
import { copy } from "@/components/config/common";
import EmailChannelModal from "./EmailChannelModal";

type MetaDraft = {
  id?: number;
  provider: "messenger" | "instagram";
  name: string;
  page_id: string;
  ig_account_id: string;
  access_token: string;
  bot_id: number | null;
};
type WebchatSettings = {
  title: string;
  greeting: string;
  color: string;
  position: "left" | "right";
  allowed_domains: string[];
  prechat: { enabled: boolean; require_email: boolean };
  // Opciones avanzadas (§19.1)
  open_selector: string | null;
  auto_open_s: number | null;
  initial_message: string | null;
  clear_on_open: boolean;
  avatar_url: string | null;
  theme: "light" | "dark" | "auto";
  launcher_text: string | null;
  header_text_color: string;
};
type WebDraft = { id?: number; name: string; bot_id: number | null; settings: WebchatSettings; domains: string };

const WEB_DEFAULTS: WebchatSettings = {
  title: "¿Te ayudamos?",
  greeting: "¡Hola! Escríbenos y te respondemos aquí mismo.",
  color: "#0f766e",
  position: "right",
  allowed_domains: [],
  prechat: { enabled: false, require_email: false },
  open_selector: null,
  auto_open_s: null,
  initial_message: null,
  clear_on_open: false,
  avatar_url: null,
  theme: "light",
  launcher_text: null,
  header_text_color: "#ffffff",
};

/** Messenger, Instagram y chat web: conexión, estado y código del widget. */
export default function OmnichannelCard({
  channels,
  bots,
  isAdmin,
  webhookUrl,
  onChange,
}: {
  channels: Channel[];
  bots: Bot[];
  isAdmin: boolean;
  webhookUrl: string;
  onChange: () => void;
}) {
  const [meta, setMeta] = useState<MetaDraft | null>(null);
  const [web, setWeb] = useState<WebDraft | null>(null);
  const [embed, setEmbed] = useState<string | null>(null);
  const [mail, setMail] = useState<{ id?: number } | null>(null);
  const [run, busy, error, setError] = useAction();

  async function saveMeta() {
    if (!meta) return;
    const { id, ...body } = meta;
    const payload = { ...body, ig_account_id: body.ig_account_id || null, access_token: body.access_token || null };
    const ok = await run(() =>
      id ? send(`/api/channels/${id}/meta`, "PUT", payload) : send("/api/channels/meta", "POST", payload),
    );
    if (ok) {
      setMeta(null);
      onChange();
    }
  }

  async function saveWeb() {
    if (!web) return;
    const settings = {
      ...web.settings,
      allowed_domains: web.domains.split(/[\s,]+/).map((d) => d.trim()).filter(Boolean),
    };
    const body = { name: web.name, bot_id: web.bot_id, settings };
    const r = await run(() =>
      web.id
        ? send<{ embed: string }>(`/api/channels/${web.id}/webchat`, "PUT", body)
        : send<{ embed: string }>("/api/channels/webchat", "POST", body),
    );
    if (r) {
      setWeb(null);
      setEmbed(r.embed);
      onChange();
    }
  }

  async function remove(c: Channel) {
    if (!confirm(`¿Quitar el canal «${c.name}»? Si tiene conversaciones se desconecta y se conserva el historial.`)) return;
    if (await run(() => send(`/api/channels/${c.id}`, "DELETE"))) onChange();
  }

  async function showEmbed(c: Channel) {
    const r = await run(() => api<{ embed: string }>(`/api/channels/${c.id}/webchat/embed`));
    if (r) setEmbed(r.embed);
  }

  const botName = (id: number | null) => bots.find((b) => b.id === id)?.name ?? "—";

  return (
    <Card
      title="Correo, Messenger, Instagram y chat web"
      actions={
        isAdmin && (
          <div className="inline">
            <button
              onClick={() => {
                setError(null);
                setMeta({ provider: "messenger", name: "", page_id: "", ig_account_id: "", access_token: "", bot_id: null });
              }}
            >
              + Messenger / Instagram
            </button>
            <button
              onClick={() => {
                setError(null);
                setWeb({ name: "Chat del sitio", bot_id: null, settings: WEB_DEFAULTS, domains: "" });
              }}
            >
              + Chat web
            </button>
            <button
              onClick={() => {
                setError(null);
                setMail({});
              }}
            >
              + Correo
            </button>
          </div>
        )
      }
    >
      <ErrorBox error={!meta && !web && !mail ? error : null} />
      {channels.length === 0 ? (
        <Empty>
          Conecta tu página de Facebook (Messenger), tu cuenta profesional de Instagram o agrega el chat a tu sitio web.
          Las conversaciones llegan a la misma bandeja, con los mismos flujos y agentes de IA.
        </Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Canal</th>
                <th>Cuenta</th>
                <th>Agente de IA</th>
                <th>Estado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {channels.map((c) => {
                const provider = (c.provider ?? "webchat") as ChannelProvider;
                return (
                  <tr key={c.id}>
                    <td>
                      {CHANNEL_ICONS[provider]} <strong>{c.name}</strong>
                      <div className="muted small">{CHANNEL_LABELS[provider]}</div>
                    </td>
                    <td className="small">
                      {provider === "webchat" ? "Widget" : provider === "email" ? c.external_id : `Página ${c.page_id ?? ""}`}
                      {provider === "instagram" && <div className="muted">IG {c.external_id}</div>}
                    </td>
                    <td className="small">{botName(c.bot_id)}</td>
                    <td>
                      {c.status === "error" ? (
                        <Badge tone="bad">Error</Badge>
                      ) : c.status === "disconnected" ? (
                        <Badge tone="warn">Desconectado</Badge>
                      ) : (
                        <Badge tone="ok">Activo</Badge>
                      )}
                      {c.last_error && <div className="small muted">{c.last_error.slice(0, 160)}</div>}
                    </td>
                    <td className="nowrap">
                      {provider === "webchat" && (
                        <button className="link small" onClick={() => showEmbed(c)}>
                          Código
                        </button>
                      )}{" "}
                      {isAdmin && (
                        <>
                          <button
                            className="link small"
                            onClick={() => {
                              setError(null);
                              if (provider === "email") {
                                setMail({ id: c.id });
                              } else if (provider === "webchat") {
                                const s = { ...WEB_DEFAULTS, ...(c.settings as Partial<WebchatSettings>) };
                                setWeb({ id: c.id, name: c.name, bot_id: c.bot_id, settings: s, domains: s.allowed_domains.join(", ") });
                              } else {
                                setMeta({
                                  id: c.id,
                                  provider: provider as "messenger" | "instagram",
                                  name: c.name,
                                  page_id: c.page_id ?? "",
                                  ig_account_id: provider === "instagram" ? c.external_id ?? "" : "",
                                  access_token: "",
                                  bot_id: c.bot_id,
                                });
                              }
                            }}
                          >
                            Editar
                          </button>{" "}
                          <button className="link small danger" onClick={() => remove(c)}>
                            Quitar
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {meta && (
        <Modal
          title={meta.id ? "Editar canal" : "Conectar Messenger o Instagram"}
          onClose={() => setMeta(null)}
          footer={
            <>
              <button onClick={() => setMeta(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !meta.name || !meta.page_id} onClick={saveMeta}>
                Guardar
              </button>
            </>
          }
        >
          <ErrorBox error={error} />
          <div className="grid2">
            <Field label="Red">
              <select
                value={meta.provider}
                disabled={!!meta.id}
                onChange={(e) => setMeta({ ...meta, provider: e.target.value as MetaDraft["provider"] })}
              >
                <option value="messenger">Facebook Messenger</option>
                <option value="instagram">Instagram Direct</option>
              </select>
            </Field>
            <Field label="Nombre">
              <input value={meta.name} onChange={(e) => setMeta({ ...meta, name: e.target.value })} placeholder="Página Tienda" />
            </Field>
            <Field label="ID de la página de Facebook">
              <input value={meta.page_id} onChange={(e) => setMeta({ ...meta, page_id: e.target.value })} />
            </Field>
            {meta.provider === "instagram" && (
              <Field label="ID de la cuenta profesional de Instagram" hint="Vinculada a esa página">
                <input
                  value={meta.ig_account_id}
                  disabled={!!meta.id}
                  onChange={(e) => setMeta({ ...meta, ig_account_id: e.target.value })}
                />
              </Field>
            )}
            <Field label="Token de acceso de la página" hint={meta.id ? "Vacío = conservar el actual" : "Se guarda cifrado (Vault)"}>
              <input
                type="password"
                value={meta.access_token}
                onChange={(e) => setMeta({ ...meta, access_token: e.target.value })}
              />
            </Field>
            <Field label="Agente de IA">
              <select
                value={meta.bot_id ?? ""}
                onChange={(e) => setMeta({ ...meta, bot_id: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">Predeterminado</option>
                {bots.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="small muted" style={{ marginTop: 12 }}>
            <strong>En Meta for Developers</strong> (la misma app de WhatsApp):
            <ol style={{ margin: "6px 0 0 18px" }}>
              <li>
                Permisos: <code>pages_messaging</code>, <code>pages_manage_metadata</code>, <code>pages_show_list</code>
                {meta.provider === "instagram" && (
                  <>
                    , <code>instagram_basic</code>, <code>instagram_manage_messages</code>
                  </>
                )}
                .
              </li>
              <li>
                Webhooks → producto «{meta.provider === "instagram" ? "Instagram" : "Page"}»: URL{" "}
                <code>{webhookUrl}</code>{" "}
                <button type="button" className="link small" onClick={() => copy(webhookUrl)}>
                  Copiar
                </button>{" "}
                con el mismo token de verificación; campos <code>messages</code>, <code>messaging_postbacks</code>,{" "}
                <code>messaging_referrals</code>, <code>message_reads</code>, <code>message_deliveries</code>.
              </li>
              <li>Genera un token de página (de un usuario del sistema, sin vencimiento) y pégalo aquí: suscribimos la app a la página.</li>
            </ol>
          </div>
        </Modal>
      )}

      {web && (
        <Modal
          title={web.id ? "Editar chat web" : "Nuevo chat web"}
          onClose={() => setWeb(null)}
          footer={
            <>
              <button onClick={() => setWeb(null)}>Cancelar</button>
              <button className="primary" disabled={busy || !web.name} onClick={saveWeb}>
                Guardar
              </button>
            </>
          }
        >
          <ErrorBox error={error} />
          <div className="grid2">
            <Field label="Nombre">
              <input value={web.name} onChange={(e) => setWeb({ ...web, name: e.target.value })} />
            </Field>
            <Field label="Agente de IA">
              <select
                value={web.bot_id ?? ""}
                onChange={(e) => setWeb({ ...web, bot_id: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">Predeterminado</option>
                {bots.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Título">
              <input value={web.settings.title} onChange={(e) => setWeb({ ...web, settings: { ...web.settings, title: e.target.value } })} />
            </Field>
            <Field label="Color">
              <input
                type="color"
                value={web.settings.color}
                onChange={(e) => setWeb({ ...web, settings: { ...web.settings, color: e.target.value } })}
              />
            </Field>
            <Field label="Saludo">
              <input
                value={web.settings.greeting}
                onChange={(e) => setWeb({ ...web, settings: { ...web.settings, greeting: e.target.value } })}
              />
            </Field>
            <Field label="Posición">
              <select
                value={web.settings.position}
                onChange={(e) => setWeb({ ...web, settings: { ...web.settings, position: e.target.value as "left" | "right" } })}
              >
                <option value="right">Abajo a la derecha</option>
                <option value="left">Abajo a la izquierda</option>
              </select>
            </Field>
          </div>
          <Field label="Dominios permitidos" hint="Separados por coma (ej. tienda.com). Vacío = cualquier sitio.">
            <input value={web.domains} onChange={(e) => setWeb({ ...web, domains: e.target.value })} placeholder="tienda.com" />
          </Field>
          <div className="inline" style={{ marginTop: 8 }}>
            <Toggle
              checked={web.settings.prechat.enabled}
              onChange={(v) => setWeb({ ...web, settings: { ...web.settings, prechat: { ...web.settings.prechat, enabled: v } } })}
              label="Pedir nombre antes de chatear"
            />
            {web.settings.prechat.enabled && (
              <Toggle
                checked={web.settings.prechat.require_email}
                onChange={(v) =>
                  setWeb({ ...web, settings: { ...web.settings, prechat: { ...web.settings.prechat, require_email: v } } })
                }
                label="Correo obligatorio"
              />
            )}
          </div>
          <details style={{ marginTop: 12 }}>
            <summary className="strong">Opciones avanzadas</summary>
            <div className="grid2" style={{ marginTop: 8 }}>
              <Field label="Texto del botón" hint="Vacío = solo el ícono">
                <input
                  value={web.settings.launcher_text ?? ""}
                  maxLength={40}
                  placeholder="¿Te ayudamos?"
                  onChange={(e) => setWeb({ ...web, settings: { ...web.settings, launcher_text: e.target.value || null } })}
                />
              </Field>
              <Field label="Tema">
                <select
                  value={web.settings.theme}
                  onChange={(e) => setWeb({ ...web, settings: { ...web.settings, theme: e.target.value as WebchatSettings["theme"] } })}
                >
                  <option value="light">Claro</option>
                  <option value="dark">Oscuro</option>
                  <option value="auto">Según el sistema del visitante</option>
                </select>
              </Field>
              <Field label="Avatar (URL https)">
                <input
                  value={web.settings.avatar_url ?? ""}
                  placeholder="https://tusitio.com/logo.png"
                  onChange={(e) => setWeb({ ...web, settings: { ...web.settings, avatar_url: e.target.value || null } })}
                />
              </Field>
              <Field label="Color del texto del encabezado">
                <input
                  type="color"
                  value={web.settings.header_text_color}
                  onChange={(e) => setWeb({ ...web, settings: { ...web.settings, header_text_color: e.target.value } })}
                />
              </Field>
              <Field label="Abrir desde botones del sitio" hint="Selector CSS, ej. .abrir-chat o #contacto">
                <input
                  value={web.settings.open_selector ?? ""}
                  placeholder=".abrir-chat"
                  onChange={(e) => setWeb({ ...web, settings: { ...web.settings, open_selector: e.target.value || null } })}
                />
              </Field>
              <Field label="Abrir solo después de (segundos)" hint="Una vez por visita. Vacío = no abrir solo.">
                <input
                  type="number"
                  min={0}
                  max={600}
                  value={web.settings.auto_open_s ?? ""}
                  onChange={(e) =>
                    setWeb({ ...web, settings: { ...web.settings, auto_open_s: e.target.value ? Number(e.target.value) : null } })
                  }
                />
              </Field>
            </div>
            <Field label="Mensaje inicial al bot" hint="Se envía al abrir el chat por primera vez para que el bot salude con contexto.">
              <input
                value={web.settings.initial_message ?? ""}
                maxLength={500}
                placeholder="Hola, vengo desde la página de vehículos nuevos"
                onChange={(e) => setWeb({ ...web, settings: { ...web.settings, initial_message: e.target.value || null } })}
              />
            </Field>
            <Toggle
              checked={web.settings.clear_on_open}
              onChange={(v) => setWeb({ ...web, settings: { ...web.settings, clear_on_open: v } })}
              label="Limpiar el chat al abrir (solo muestra los mensajes nuevos)"
            />
          </details>
        </Modal>
      )}

      {mail && (
        <EmailChannelModal
          channelId={mail.id}
          bots={bots}
          onClose={() => setMail(null)}
          onSaved={onChange}
        />
      )}

      {embed && (
        <Modal title="Código del chat web" onClose={() => setEmbed(null)}>
          <p className="small">
            Pega esta línea antes de <code>&lt;/body&gt;</code> en tu sitio (o en una etiqueta HTML personalizada de Google Tag
            Manager). Si el sitio también tiene el script de Atribución web, la conversación queda atribuida a la visita.
          </p>
          <pre className="code" style={{ whiteSpace: "pre-wrap" }}>{embed}</pre>
          <button className="primary" onClick={() => copy(embed)}>
            Copiar
          </button>
        </Modal>
      )}
    </Card>
  );
}
