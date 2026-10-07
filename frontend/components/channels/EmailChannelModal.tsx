"use client";

import { useEffect, useState } from "react";
import { api, send, type Bot } from "@/lib/api";
import { ErrorBox, Field, Modal, Toggle, useAction } from "@/components/ui";
import { copy } from "@/components/config/common";

type Inbound = "forward" | "provider" | "imap";

type EmailChannel = {
  id: number;
  name: string;
  address: string;
  status: string;
  last_error: string | null;
  bot_id: number | null;
  inbound: Inbound;
  inbound_url: string;
  forward_address: string | null;
  smtp: { host?: string; port?: number; user?: string | null; starttls?: boolean; has_password: boolean };
  imap: { host?: string; port?: number; user?: string | null; folder?: string; ssl?: boolean; has_password: boolean } | null;
  from_name: string | null;
  signature_html: string | null;
  group_id: number | null;
  auto_reply: string | null;
  ai_replies: boolean;
  mailgun_signing: boolean;
};

type Draft = {
  name: string;
  address: string;
  inbound: Inbound;
  bot_id: number | null;
  smtp: { host: string; port: number; user: string; password: string; starttls: boolean };
  imap: { host: string; port: number; user: string; password: string; folder: string; ssl: boolean };
  from_name: string;
  signature_html: string;
  auto_reply: string;
  ai_replies: boolean;
  mailgun_signing_key: string;
};

type Setup = { inbound_url: string; providers: { provider: string; steps: string }[] };
type TestResult = { smtp?: { ok: boolean; error?: string }; imap?: { ok: boolean; error?: string } };

const EMPTY: Draft = {
  name: "Correo",
  address: "",
  inbound: "provider",
  bot_id: null,
  smtp: { host: "", port: 587, user: "", password: "", starttls: true },
  imap: { host: "", port: 993, user: "", password: "", folder: "INBOX", ssl: true },
  from_name: "",
  signature_html: "",
  auto_reply: "",
  ai_replies: true,
  mailgun_signing_key: "",
};

const PROVIDER_LABEL: Record<string, string> = { postmark: "Postmark", sendgrid: "SendGrid", mailgun: "Mailgun", imap: "IMAP" };

/** Alta / edición del canal de correo: SMTP para responder y webhook o IMAP para recibir. */
export default function EmailChannelModal({
  channelId,
  bots,
  onClose,
  onSaved,
}: {
  channelId?: number;
  bots: Bot[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [saved, setSaved] = useState<EmailChannel | null>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [run, busy, error] = useAction();

  useEffect(() => {
    if (!channelId) return;
    api<EmailChannel>(`/api/channels/${channelId}/email`).then((c) => {
      setSaved(c);
      setDraft({
        name: c.name,
        address: c.address,
        inbound: c.inbound,
        bot_id: c.bot_id,
        smtp: { host: c.smtp.host ?? "", port: c.smtp.port ?? 587, user: c.smtp.user ?? "", password: "", starttls: c.smtp.starttls ?? true },
        imap: {
          host: c.imap?.host ?? "", port: c.imap?.port ?? 993, user: c.imap?.user ?? "", password: "",
          folder: c.imap?.folder ?? "INBOX", ssl: c.imap?.ssl ?? true,
        },
        from_name: c.from_name ?? "",
        signature_html: c.signature_html ?? "",
        auto_reply: c.auto_reply ?? "",
        ai_replies: c.ai_replies,
        mailgun_signing_key: "",
      });
    });
  }, [channelId]);

  useEffect(() => {
    const id = saved?.id;
    if (id) api<Setup>(`/api/channels/${id}/email/setup`).then(setSetup).catch(() => setSetup(null));
  }, [saved?.id]);

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  async function save() {
    const body = {
      name: draft.name,
      address: draft.address,
      inbound: draft.inbound,
      bot_id: draft.bot_id,
      smtp: draft.smtp.host ? { ...draft.smtp, user: draft.smtp.user || null, password: draft.smtp.password || null } : null,
      imap: draft.inbound === "imap" ? { ...draft.imap, user: draft.imap.user || null, password: draft.imap.password || null } : null,
      from_name: draft.from_name || null,
      signature_html: draft.signature_html || null,
      auto_reply: draft.auto_reply || null,
      ai_replies: draft.ai_replies,
      mailgun_signing_key: draft.mailgun_signing_key || null,
    };
    const r = await run(() =>
      saved
        ? send<EmailChannel>(`/api/channels/${saved.id}/email`, "PUT", body)
        : send<EmailChannel>("/api/channels/email", "POST", body),
    );
    if (r) {
      setSaved(r);
      onSaved();
    }
  }

  async function runTest() {
    if (!saved) return;
    setTest(null);
    const r = await run(() => send<TestResult>(`/api/channels/${saved.id}/email/test`, "POST"));
    if (r) setTest(r);
  }

  return (
    <Modal
      title={saved ? `Correo · ${saved.address}` : "Nuevo canal de correo"}
      onClose={onClose}
      footer={
        <>
          {saved && (
            <button onClick={runTest} disabled={busy}>
              Probar conexión
            </button>
          )}
          <button onClick={onClose}>Cerrar</button>
          <button className="primary" disabled={busy || !draft.name || !draft.address} onClick={save}>
            Guardar
          </button>
        </>
      }
    >
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Nombre">
          <input value={draft.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Dirección del buzón">
          <input type="email" value={draft.address} placeholder="ventas@tuempresa.com" onChange={(e) => set("address", e.target.value)} />
        </Field>
        <Field label="Nombre del remitente">
          <input value={draft.from_name} placeholder="Ventas · Tu empresa" onChange={(e) => set("from_name", e.target.value)} />
        </Field>
        <Field label="Agente de IA">
          <select value={draft.bot_id ?? ""} onChange={(e) => set("bot_id", e.target.value ? Number(e.target.value) : null)}>
            <option value="">Predeterminado</option>
            {bots.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <h4 style={{ margin: "12px 0 4px" }}>Responder (SMTP)</h4>
      <div className="grid2">
        <Field label="Servidor SMTP">
          <input value={draft.smtp.host} placeholder="smtp.gmail.com" onChange={(e) => set("smtp", { ...draft.smtp, host: e.target.value })} />
        </Field>
        <Field label="Puerto" hint="587 (STARTTLS) o 465 (SSL)">
          <input type="number" value={draft.smtp.port} onChange={(e) => set("smtp", { ...draft.smtp, port: Number(e.target.value) || 587 })} />
        </Field>
        <Field label="Usuario">
          <input value={draft.smtp.user} autoComplete="off" onChange={(e) => set("smtp", { ...draft.smtp, user: e.target.value })} />
        </Field>
        <Field label="Contraseña de aplicación" hint={saved?.smtp.has_password ? "Guardada; vacío = conservar" : undefined}>
          <input type="password" autoComplete="new-password" value={draft.smtp.password}
            onChange={(e) => set("smtp", { ...draft.smtp, password: e.target.value })} />
        </Field>
      </div>
      <Toggle checked={draft.smtp.starttls} onChange={(v) => set("smtp", { ...draft.smtp, starttls: v })} label="STARTTLS" />

      <h4 style={{ margin: "12px 0 4px" }}>Recibir</h4>
      <Field label="¿Cómo llegan los correos?">
        <select value={draft.inbound} onChange={(e) => set("inbound", e.target.value as Inbound)}>
          <option value="provider">Webhook de un proveedor (Postmark, SendGrid o Mailgun)</option>
          <option value="forward">Reenvío del buzón a la dirección de entrada</option>
          <option value="imap">Leer el buzón por IMAP</option>
        </select>
      </Field>
      {draft.inbound === "imap" && (
        <div className="grid2">
          <Field label="Servidor IMAP">
            <input value={draft.imap.host} placeholder="imap.gmail.com" onChange={(e) => set("imap", { ...draft.imap, host: e.target.value })} />
          </Field>
          <Field label="Puerto">
            <input type="number" value={draft.imap.port} onChange={(e) => set("imap", { ...draft.imap, port: Number(e.target.value) || 993 })} />
          </Field>
          <Field label="Usuario">
            <input value={draft.imap.user} autoComplete="off" onChange={(e) => set("imap", { ...draft.imap, user: e.target.value })} />
          </Field>
          <Field label="Contraseña" hint={saved?.imap?.has_password ? "Guardada; vacío = conservar" : undefined}>
            <input type="password" autoComplete="new-password" value={draft.imap.password}
              onChange={(e) => set("imap", { ...draft.imap, password: e.target.value })} />
          </Field>
          <Field label="Carpeta">
            <input value={draft.imap.folder} onChange={(e) => set("imap", { ...draft.imap, folder: e.target.value })} />
          </Field>
        </div>
      )}
      {draft.inbound === "provider" && (
        <Field label="Clave de firma de Mailgun (opcional)" hint={saved?.mailgun_signing ? "Guardada; vacío = conservar" : "Valida que el webhook venga de Mailgun"}>
          <input type="password" autoComplete="off" value={draft.mailgun_signing_key}
            onChange={(e) => set("mailgun_signing_key", e.target.value)} />
        </Field>
      )}

      <h4 style={{ margin: "12px 0 4px" }}>Respuestas</h4>
      <Field label="Firma (HTML simple)">
        <textarea rows={3} value={draft.signature_html} placeholder="<b>Equipo de ventas</b><br>Tu empresa"
          onChange={(e) => set("signature_html", e.target.value)} />
      </Field>
      <Field label="Respuesta automática al recibir un correo nuevo (opcional)">
        <textarea rows={2} value={draft.auto_reply} placeholder="Recibimos tu correo, te respondemos pronto."
          onChange={(e) => set("auto_reply", e.target.value)} />
      </Field>
      <Toggle checked={draft.ai_replies} onChange={(v) => set("ai_replies", v)} label="El agente de IA responde los correos" />

      {saved && (
        <div className="card-sub" style={{ marginTop: 12 }}>
          <h4 style={{ margin: "0 0 4px" }}>Conexión de entrada</h4>
          <p className="small">
            URL del webhook: <code>{saved.inbound_url}</code>{" "}
            <button type="button" className="link small" onClick={() => copy(saved.inbound_url)}>
              Copiar
            </button>
          </p>
          {saved.forward_address && (
            <p className="small">
              Reenvía tu buzón a <code>{saved.forward_address}</code>{" "}
              <button type="button" className="link small" onClick={() => copy(saved.forward_address ?? "")}>
                Copiar
              </button>
            </p>
          )}
          {setup && (
            <ul className="small">
              {setup.providers.map((p) => (
                <li key={p.provider}>
                  <strong>{PROVIDER_LABEL[p.provider] ?? p.provider}:</strong> {p.steps}
                </li>
              ))}
            </ul>
          )}
          {test && (
            <p className="small" role="status">
              {test.smtp && <>SMTP: {test.smtp.ok ? "✅ conectado" : `❌ ${test.smtp.error}`}. </>}
              {test.imap && <>IMAP: {test.imap.ok ? "✅ conectado" : `❌ ${test.imap.error}`}.</>}
              {!test.smtp && !test.imap && "Configura SMTP o IMAP para probar."}
            </p>
          )}
          {saved.last_error && <p className="small error">{saved.last_error}</p>}
        </div>
      )}
    </Modal>
  );
}
