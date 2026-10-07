"use client";

import { useState } from "react";
import { API_URL, api, send, type Bot, type Channel, type Template } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, copy, useIsAdmin } from "@/components/config/common";
import { EmbeddedSignupButton } from "@/components/saas/EmbeddedSignupButton";
import OmnichannelCard from "@/components/channels/OmnichannelCard";

type ChannelsResp = { channels: Channel[]; waba_id: string | null; webhook_path: string; app_secret_configured: boolean };
type Draft = { id?: number; name: string; phone_number_id: string; display_phone: string; access_token: string; bot_id: number | null };

function Check({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <li className="inline" style={{ alignItems: "flex-start" }}>
      <span style={{ color: ok ? "var(--ok)" : "var(--warn)" }}>{ok ? "✔" : "⚠"}</span>
      <span>{children}</span>
    </li>
  );
}

export default function PlataformaPage() {
  const isAdmin = useIsAdmin();
  const resp = useApi<ChannelsResp>("/api/channels");
  const { error, loading, reload } = resp;
  // Esta tarjeta es de WhatsApp; Messenger, Instagram y chat web están en OmnichannelCard
  const data = resp.data && {
    ...resp.data,
    channels: resp.data.channels.filter((c) => !c.provider || c.provider === "whatsapp_cloud"),
  };
  const bots = useApi<Bot[]>("/api/bots").data ?? [];
  const [draft, setDraft] = useState<Draft | null>(null);
  const [run, busy, actionError, setActionError] = useAction();
  const [templatesInfo, setTemplatesInfo] = useState<string | null>(null);

  const webhookUrl = `${API_URL}${data?.webhook_path ?? "/webhooks/whatsapp"}`;

  async function save() {
    if (!draft) return;
    const { id, ...body } = draft;
    const payload = { ...body, access_token: body.access_token || null, display_phone: body.display_phone || null };
    const ok = await run(() => (id ? send(`/api/channels/${id}`, "PUT", payload) : send("/api/channels", "POST", payload)));
    if (ok) {
      setDraft(null);
      reload();
    }
  }

  async function refreshTemplates() {
    const r = await run(() => api<Template[]>("/api/templates?refresh=true"));
    if (r) {
      const approved = r.filter((t) => t.status === "APPROVED").length;
      setTemplatesInfo(`${r.length} plantillas en Meta · ${approved} aprobadas`);
    }
  }

  const botName = (id: number | null) => bots.find((b) => b.id === id)?.name ?? "—";

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Plataforma: números de WhatsApp y conexión con Meta." />
      <ConfigTabs />
      <AdminNotice />
      <ErrorBox error={error || (!draft ? actionError : null)} />

      <Card
        title="Números de WhatsApp"
        actions={
          isAdmin && (
            <>
            <EmbeddedSignupButton onConnected={reload} />
            <button
              className="primary"
              onClick={() => {
                setActionError(null);
                setDraft({ name: "", phone_number_id: "", display_phone: "", access_token: "", bot_id: bots[0]?.id ?? null });
              }}
            >
              Agregar número
            </button>
            </>
          )
        }
      >
        {loading && !data ? (
          <Loading />
        ) : !data?.channels.length ? (
          <Empty>No hay números. Define WA_PHONE_NUMBER_ID en el servidor o agrega uno aquí.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Nombre</th><th>Número</th><th>Phone number ID</th><th>Token</th><th>Agente</th><th /></tr>
              </thead>
              <tbody>
                {data.channels.map((c) => (
                  <tr key={c.id}>
                    <td className="strong">{c.name}</td>
                    <td>{c.display_phone ?? <span className="muted">—</span>}</td>
                    <td><code className="small">{c.phone_number_id}</code></td>
                    <td>
                      {!c.token_configured ? <Badge tone="bad">Sin token</Badge>
                        : c.has_own_token ? <Badge tone="ok">Propio</Badge>
                        : <Badge tone="ok">Del servidor</Badge>}
                    </td>
                    <td>{botName(c.bot_id)}</td>
                    <td>
                      {isAdmin && (
                        <button
                          onClick={() => {
                            setActionError(null);
                            setDraft({
                              id: c.id, name: c.name, phone_number_id: c.phone_number_id ?? "",
                              display_phone: c.display_phone ?? "", access_token: "", bot_id: c.bot_id,
                            });
                          }}
                        >
                          Editar
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

      <OmnichannelCard
        channels={(resp.data?.channels ?? []).filter((c) => c.provider && c.provider !== "whatsapp_cloud")}
        bots={bots}
        isAdmin={isAdmin}
        webhookUrl={webhookUrl}
        onChange={reload}
      />

      <Card title="Conexión con Meta">
        <ul className="stack small" style={{ listStyle: "none", padding: 0, margin: 0, gap: 8 }}>
          <Check ok={!!data?.channels.some((c) => c.token_configured)}>
            Token de acceso permanente (System User con <code>whatsapp_business_messaging</code> y{" "}
            <code>whatsapp_business_management</code>).
          </Check>
          <Check ok={!!data?.waba_id}>
            WhatsApp Business Account ID {data?.waba_id ? <code>{data.waba_id}</code> : "(WA_WABA_ID) — necesario para plantillas y campañas"}.
          </Check>
          <Check ok={!!data?.app_secret_configured}>
            App Secret (WA_APP_SECRET) para validar la firma de los webhooks de Meta.
          </Check>
          <li>
            <div className="strong">URL del webhook</div>
            <div className="inline">
              <code>{webhookUrl}</code>
              <button className="link small" onClick={() => copy(webhookUrl)}>Copiar</button>
            </div>
            <div className="muted">
              Configúrala en Meta (WhatsApp → Configuración) con el token de verificación WA_VERIFY_TOKEN y suscribe los
              campos: messages, message_template_status_update, message_template_quality_update,
              phone_number_quality_update, account_update y account_alerts.
            </div>
          </li>
        </ul>
        <div className="inline" style={{ marginTop: 12 }}>
          <button onClick={refreshTemplates} disabled={busy}>Actualizar plantillas desde Meta</button>
          {templatesInfo && <span className="muted small">{templatesInfo}</span>}
        </div>
      </Card>

      {draft && (
        <Modal
          title={draft.id ? "Editar número" : "Agregar número"}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button onClick={() => setDraft(null)}>Cancelar</button>
              <button className="primary" onClick={save} disabled={busy || !draft.name.trim() || !draft.phone_number_id.trim()}>
                Guardar
              </button>
            </>
          }
        >
          <Field label="Nombre"><input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Field>
          <Field label="Número visible" hint="Ej. +57 300 123 4567">
            <input value={draft.display_phone} onChange={(e) => setDraft({ ...draft, display_phone: e.target.value })} />
          </Field>
          <Field label="Phone number ID">
            <input
              value={draft.phone_number_id}
              disabled={!!draft.id}
              onChange={(e) => setDraft({ ...draft, phone_number_id: e.target.value })}
            />
          </Field>
          <Field label="Token de acceso (opcional)" hint={draft.id ? "Déjalo vacío para conservar el actual." : "Vacío = usa el token del servidor."}>
            <input type="password" value={draft.access_token} onChange={(e) => setDraft({ ...draft, access_token: e.target.value })} />
          </Field>
          <Field label="Agente de IA">
            <select value={draft.bot_id ?? ""} onChange={(e) => setDraft({ ...draft, bot_id: e.target.value ? Number(e.target.value) : null })}>
              {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </Field>
          <ErrorBox error={actionError} />
        </Modal>
      )}
    </>
  );
}
