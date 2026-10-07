"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import QRCode from "qrcode";
import { fmtDateTime } from "@/lib/api";
import { loadMe, post, rawJson, SecurityError, setDeviceToken, type MeInfo } from "@/lib/security";
import { Badge, Card, Empty, ErrorBox, Field, Modal, PageHeader } from "@/components/ui";
import ChangePasswordForm from "@/components/security/ChangePasswordForm";

type Device = { id: number; ip: string | null; user_agent: string | null; last_used_at: string; expires_at: string };

function RecoveryCodes({ codes, onClose }: { codes: string[]; onClose: () => void }) {
  return (
    <Modal title="Guarda tus códigos de recuperación" onClose={onClose}
      footer={<button className="primary" onClick={onClose}>Ya los guardé</button>}>
      <p className="muted">
        Cada código sirve una sola vez si pierdes tu teléfono o no te llega el correo. No los volveremos a mostrar.
      </p>
      <pre style={{ fontSize: 16, columns: 2, lineHeight: 1.8 }} aria-label="Códigos de recuperación">
        {codes.join("\n")}
      </pre>
      <button onClick={() => navigator.clipboard?.writeText(codes.join("\n"))}>Copiar</button>
    </Modal>
  );
}

function TotpSetup({ onEnabled, onCancel }: { onEnabled: (codes: string[]) => void; onCancel: () => void }) {
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string; setup_token: string } | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    post<{ secret: string; otpauth_uri: string; setup_token: string }>("/api/auth/mfa/totp/setup", {}, true)
      .then(async (s) => {
        setSetup(s);
        setQr(await QRCode.toDataURL(s.otpauth_uri, { margin: 1, width: 220 }));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "No se pudo iniciar"));
  }, []);

  async function enable(e: React.FormEvent) {
    e.preventDefault();
    if (!setup) return;
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ recovery_codes: string[] }>("/api/auth/mfa/totp/enable",
        { setup_token: setup.setup_token, code }, true);
      onEnabled(r.recovery_codes);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Código incorrecto");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Activar app autenticadora" onClose={onCancel}>
      <form onSubmit={enable} style={{ display: "grid", gap: 12 }}>
        <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
          <li>Abre Google Authenticator, Microsoft Authenticator o Authy.</li>
          <li>Escanea el código QR (o escribe la clave manualmente).</li>
          <li>Escribe el código de 6 dígitos que aparece.</li>
        </ol>
        {qr ? <img src={qr} alt="Código QR para la app autenticadora" width={220} height={220} /> : <p>Cargando…</p>}
        {setup && (
          <p className="small">
            Clave manual: <code style={{ userSelect: "all" }}>{setup.secret.match(/.{1,4}/g)?.join(" ")}</code>
          </p>
        )}
        <Field label="Código de 6 dígitos">
          <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6}
            pattern="\d{6}" autoComplete="one-time-code" required autoFocus />
        </Field>
        <ErrorBox error={error} />
        <div className="inline">
          <button className="primary" disabled={busy || !setup}>{busy ? "Verificando…" : "Activar"}</button>
          <button type="button" onClick={onCancel}>Cancelar</button>
        </div>
      </form>
    </Modal>
  );
}

function PasswordPrompt({ title, onSubmit, onCancel }: {
  title: string;
  onSubmit: (password: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title={title} onClose={onCancel}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          try {
            await onSubmit(password);
          } catch (err) {
            setError(err instanceof Error ? err.message : "No se pudo completar");
          }
        }}
        style={{ display: "grid", gap: 12 }}
      >
        <Field label="Confirma con tu contraseña">
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus
            autoComplete="current-password" />
        </Field>
        <ErrorBox error={error} />
        <div className="inline">
          <button className="primary">Confirmar</button>
          <button type="button" onClick={onCancel}>Cancelar</button>
        </div>
      </form>
    </Modal>
  );
}

function SecurityPage() {
  const params = useSearchParams();
  const mandatory = params.get("obligatorio") === "1";
  const [me, setMe] = useState<MeInfo | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [prompt, setPrompt] = useState<"disable" | "codes" | null>(null);

  async function refresh() {
    setMe(await loadMe(true));
    setDevices(await rawJson<Device[]>("/api/auth/devices", {}, true).catch(() => []));
  }
  useEffect(() => {
    refresh().catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  async function enableEmail() {
    try {
      const r = await post<{ recovery_codes: string[] }>("/api/auth/mfa/email/enable", {}, true);
      setCodes(r.recovery_codes);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo activar");
    }
  }

  if (!me) return <ErrorBox error={error} />;
  return (
    <>
      <PageHeader title="Mi seguridad" subtitle="Tu contraseña, el segundo factor de acceso y los dispositivos de confianza." />
      {mandatory && !me.mfa.enabled && (
        <p className="notice" role="alert">
          Tu empresa exige el segundo factor de acceso. Actívalo para continuar usando el panel.
        </p>
      )}
      <ErrorBox error={error} />
      <Card
        title="Verificación en dos pasos (2FA)"
        actions={me.mfa.enabled ? <Badge tone="ok">Activa · {me.mfa.method === "totp" ? "App" : "Correo"}</Badge>
          : <Badge tone={me.mfa.required ? "bad" : "neutral"}>{me.mfa.required ? "Obligatoria" : "Inactiva"}</Badge>}
      >
        {me.mfa.enabled ? (
          <div style={{ display: "grid", gap: 8 }}>
            <p className="muted small">
              Te quedan {me.mfa.recovery_codes_left} códigos de recuperación.
            </p>
            <div className="inline">
              <button onClick={() => setPrompt("codes")}>Generar códigos nuevos</button>
              {!me.mfa.required && (
                <button className="danger" onClick={() => setPrompt("disable")}>Desactivar</button>
              )}
              {me.mfa.method === "email" && <button onClick={() => setSetupOpen(true)}>Cambiar a app autenticadora</button>}
            </div>
          </div>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            <p className="muted small">
              Además de tu contraseña te pediremos un código. Así nadie entra aunque conozca tu contraseña.
            </p>
            <div className="inline">
              <button className="primary" onClick={() => setSetupOpen(true)}>Usar app autenticadora (recomendado)</button>
              <button onClick={enableEmail}>Recibir el código por correo</button>
            </div>
          </div>
        )}
      </Card>
      {me.has_password && (
        <Card title="Contraseña">
          <ChangePasswordForm />
        </Card>
      )}
      <Card
        title="Dispositivos de confianza"
        actions={devices.length > 0 && (
          <button onClick={async () => {
            await rawJson("/api/auth/devices", { method: "DELETE" }, true);
            setDeviceToken(null);
            await refresh();
          }}>Olvidar todos</button>
        )}
      >
        {devices.length === 0 ? <Empty>Ningún navegador recordado.</Empty> : (
          <table className="table">
            <thead><tr><th>Navegador</th><th>IP</th><th>Último uso</th><th>Vence</th></tr></thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.id}>
                  <td className="small">{(d.user_agent ?? "—").slice(0, 80)}</td>
                  <td>{d.ip ?? "—"}</td>
                  <td>{fmtDateTime(d.last_used_at)}</td>
                  <td>{fmtDateTime(d.expires_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {setupOpen && (
        <TotpSetup onCancel={() => setSetupOpen(false)} onEnabled={async (c) => {
          setSetupOpen(false);
          setCodes(c);
          await refresh();
        }} />
      )}
      {codes && <RecoveryCodes codes={codes} onClose={() => setCodes(null)} />}
      {prompt && (
        <PasswordPrompt
          title={prompt === "disable" ? "Desactivar el segundo factor" : "Códigos de recuperación nuevos"}
          onCancel={() => setPrompt(null)}
          onSubmit={async (password) => {
            try {
              if (prompt === "disable") await post("/api/auth/mfa/disable", { password }, true);
              else setCodes((await post<{ recovery_codes: string[] }>("/api/auth/mfa/recovery-codes", { password }, true)).recovery_codes);
            } catch (err) {
              throw err instanceof SecurityError ? err : new Error("No se pudo completar");
            }
            setPrompt(null);
            await refresh();
          }}
        />
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={null}>
      <SecurityPage />
    </Suspense>
  );
}
