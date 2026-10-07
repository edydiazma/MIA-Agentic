"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { API_URL, setSession } from "@/lib/api";
import {
  getDeviceToken,
  post,
  rawJson,
  SecurityError,
  setDeviceToken,
  type LoginResult,
} from "@/lib/security";

type Mfa = { token: string; methods: string[]; emailHint: string | null };
type Sso = { slug: string; name: string; enforce: boolean };

function afterLogin(router: ReturnType<typeof useRouter>, r: Extract<LoginResult, { access_token: string }>) {
  setSession(r.access_token, r.agent);
  if (r.device_token) setDeviceToken(r.device_token);
  try {
    // Nueva sesión: vuelve a revisar si falta el asistente de configuración
    sessionStorage.removeItem("onboarding_checked");
    sessionStorage.removeItem("onboarding_skip_redirect");
  } catch {}
  if (r.must_change_password) router.replace("/cambiar-clave");
  else if (r.mfa_enrollment_required) router.replace("/perfil/seguridad?obligatorio=1");
  else router.replace("/");
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(params.get("sso_error"));
  const [loading, setLoading] = useState(false);
  const [orgs, setOrgs] = useState<{ id: number; name: string }[] | null>(null);
  const [mfa, setMfa] = useState<Mfa | null>(null);
  const [code, setCode] = useState("");
  const [remember, setRemember] = useState(true);
  const [useRecovery, setUseRecovery] = useState(false);
  const [sso, setSso] = useState<Sso | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // ¿El dominio del correo usa inicio de sesión único?
  useEffect(() => {
    const value = email.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
      setSso(null);
      return;
    }
    const t = setTimeout(() => {
      rawJson<{ sso: Sso | null }>(`/api/auth/sso/discover?email=${encodeURIComponent(value)}`)
        .then((r) => setSso(r.sso))
        .catch(() => setSso(null));
    }, 400);
    return () => clearTimeout(t);
  }, [email]);

  function goSso(slug: string) {
    window.location.href = `${API_URL}/auth/sso/${encodeURIComponent(slug)}/start`;
  }

  async function submit(e: React.FormEvent | null, organizationId?: number) {
    e?.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const r = await post<LoginResult>("/api/auth/login", {
        email,
        password,
        organization_id: organizationId ?? null,
        device_token: getDeviceToken(),
      });
      if ("choose_org" in r) setOrgs(r.choose_org);
      else if ("mfa_required" in r) {
        setMfa({ token: r.mfa_token, methods: r.methods, emailHint: r.email_hint });
        setCode("");
      } else afterLogin(router, r);
    } catch (err) {
      if (err instanceof SecurityError && err.status === 403 && (err.detail as { sso?: Sso })?.sso) {
        const s = (err.detail as { sso: Sso }).sso;
        setSso({ ...s, enforce: true });
        setError("Tu empresa usa inicio de sesión único. Continúa con tu proveedor.");
      } else setError(err instanceof Error ? err.message : "Error al iniciar sesión");
    } finally {
      setLoading(false);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    if (!mfa) return;
    setLoading(true);
    setError(null);
    try {
      const r = await post<Extract<LoginResult, { access_token: string }>>("/api/auth/mfa/verify", {
        mfa_token: mfa.token,
        code,
        remember_device: remember,
        device_token: getDeviceToken(),
      });
      afterLogin(router, r);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Código incorrecto";
      setError(msg);
      if (err instanceof SecurityError && /venció|inicia sesión/i.test(msg)) setMfa(null);
    } finally {
      setLoading(false);
    }
  }

  async function resend() {
    if (!mfa) return;
    try {
      await post("/api/auth/mfa/resend", { mfa_token: mfa.token });
      setNotice("Te enviamos un código nuevo.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo reenviar");
    }
  }

  if (mfa) {
    const primary = mfa.methods[0];
    const label = useRecovery
      ? "Código de recuperación"
      : primary === "totp"
        ? "Código de tu app autenticadora"
        : `Código enviado a ${mfa.emailHint ?? "tu correo"}`;
    return (
      <main className="login">
        <form onSubmit={verify} className="card" aria-labelledby="mfa-title">
          <h1 id="mfa-title">Verificación en dos pasos</h1>
          <p className="muted">
            {useRecovery
              ? "Usa uno de los códigos de recuperación que guardaste al activar el segundo factor."
              : primary === "totp"
                ? "Abre tu app autenticadora (Google Authenticator, Microsoft Authenticator, Authy) y escribe el código de 6 dígitos."
                : "Escribe el código de 6 dígitos que te enviamos. Vence en 10 minutos."}
          </p>
          <label>
            {label}
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode={useRecovery ? "text" : "numeric"}
              autoComplete="one-time-code"
              autoFocus
              required
              maxLength={useRecovery ? 20 : 6}
              pattern={useRecovery ? undefined : "\\d{6}"}
            />
          </label>
          <label className="inline">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
            Recordar este dispositivo
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {notice && <p className="small muted">{notice}</p>}
          <button className="primary" disabled={loading}>
            {loading ? "Verificando…" : "Verificar"}
          </button>
          <div className="inline small" style={{ justifyContent: "space-between" }}>
            {primary === "email" && !useRecovery && (
              <button type="button" className="link small" onClick={resend}>
                Reenviar código
              </button>
            )}
            {mfa.methods.includes("recovery") && (
              <button type="button" className="link small" onClick={() => setUseRecovery((v) => !v)}>
                {useRecovery ? "Usar el código normal" : "Usar un código de recuperación"}
              </button>
            )}
            <button type="button" className="link small" onClick={() => setMfa(null)}>
              Volver
            </button>
          </div>
        </form>
      </main>
    );
  }

  if (orgs)
    return (
      <main className="login">
        <div className="card">
          <h1>Elige la empresa</h1>
          <p className="muted">Tu correo tiene acceso a varias empresas. Podrás cambiar luego desde la barra superior.</p>
          {orgs.map((o) => (
            <button key={o.id} disabled={loading} onClick={() => submit(null, o.id)} style={{ textAlign: "left" }}>
              {o.name}
            </button>
          ))}
          {error && <p className="error">{error}</p>}
          <button className="link" onClick={() => setOrgs(null)}>
            Volver
          </button>
        </div>
      </main>
    );

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <h1>Bandeja WhatsApp</h1>
        <p className="muted">Ingresa con tu cuenta de asesor</p>
        <label>
          Correo
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
            autoComplete="username"
          />
        </label>
        {!sso?.enforce && (
          <label>
            Contraseña
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </label>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {!sso?.enforce && (
          <button className="primary" disabled={loading}>
            {loading ? "Ingresando…" : "Ingresar"}
          </button>
        )}
        {sso && (
          <button type="button" className={sso.enforce ? "primary" : ""} onClick={() => goSso(sso.slug)}>
            Ingresar con {sso.name || "SSO"}
          </button>
        )}
        <div className="inline small" style={{ justifyContent: "space-between" }}>
          <Link href="/recuperar">¿Olvidaste tu contraseña?</Link>
          <span className="muted">
            ¿Sin cuenta? <Link href="/signup">Regístrate</Link>
          </span>
        </div>
      </form>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
