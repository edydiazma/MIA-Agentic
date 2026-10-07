"use client";

import { useEffect, useRef, useState } from "react";
import { api, send, type Channel } from "@/lib/api";
import { ErrorBox, Field, Toggle, useAction } from "@/components/ui";
import { copy } from "@/components/config/common";
import { StepFooter, useWizard } from "./common";

type FB = {
  init: (o: Record<string, unknown>) => void;
  login: (cb: (r: { authResponse?: { code?: string } | null; status?: string }) => void, o: Record<string, unknown>) => void;
};
type FBWindow = { FB?: FB; fbAsyncInit?: () => void };
const fbWindow = () => window as unknown as FBWindow;

function loadSdk(appId: string, version: string): Promise<FB> {
  return new Promise((resolve, reject) => {
    const w = fbWindow();
    if (w.FB) {
      w.FB.init({ appId, autoLogAppEvents: true, xfbml: false, version });
      return resolve(w.FB);
    }
    w.fbAsyncInit = () => {
      w.FB!.init({ appId, autoLogAppEvents: true, xfbml: false, version });
      resolve(w.FB!);
    };
    const s = document.createElement("script");
    s.src = "https://connect.facebook.net/en_US/sdk.js";
    s.async = true;
    s.defer = true;
    s.crossOrigin = "anonymous";
    s.onerror = () => reject(new Error("No se pudo cargar el SDK de Facebook. Revisa tu conexión o el bloqueador de anuncios."));
    document.body.appendChild(s);
  });
}

type SessionInfo = { phone_number_id?: string; waba_id?: string; business_id?: string };
type Connected = { channel: { id: number; name: string; display_phone: string | null; phone_number_id: string; waba_id: string; verified_name: string | null }; pin: string | null };

const CANCEL_STEP: Record<string, string> = {
  BUSINESS_ACCOUNT_SELECTION: "al elegir el portafolio de negocio",
  WABA_SELECTION: "al elegir la cuenta de WhatsApp Business",
  PHONE_NUMBER_SETUP: "al agregar el número",
  PHONE_NUMBER_VERIFICATION: "al verificar el número con el código",
  PERMISSIONS: "al aceptar los permisos",
};

export default function WhatsAppStep() {
  const { state, answers, update, reload, next, goTo } = useWizard();
  const cfg = state.embedded_signup;
  const coexistence = !!answers.whatsapp?.coexistence;
  const session = useRef<SessionInfo>({});
  const [phase, setPhase] = useState<"idle" | "meta" | "linking">("idle");
  const [result, setResult] = useState<Connected | null>(null);
  const [cancelInfo, setCancelInfo] = useState<string | null>(null);
  const [run, busy, error, setError] = useAction();
  const [manual, setManual] = useState({ name: "", phone_number_id: "", waba_id: "", display_phone: "", access_token: "" });
  const [existing, setExisting] = useState<Channel | null>(null);

  useEffect(() => {
    if (!state.run?.channel_id) return;
    api<{ channels: Channel[] }>("/api/channels").then(
      (r) => setExisting(r.channels.find((c) => c.id === state.run?.channel_id) ?? null),
      () => setExisting(null),
    );
  }, [state.run?.channel_id]);

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      let host = "";
      try {
        host = new URL(e.origin).hostname;
      } catch {
        return;
      }
      if (!/(^|\.)facebook\.com$/.test(host)) return;
      let d: { type?: string; event?: string; data?: SessionInfo & { current_step?: string; error_message?: string } } | null = null;
      try {
        d = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
      } catch {
        return;
      }
      if (d?.type !== "WA_EMBEDDED_SIGNUP") return;
      if (d.event === "FINISH" || d.event === "FINISH_ONLY_WABA" || d.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") {
        session.current = { ...session.current, ...d.data };
      } else if (d.event === "CANCEL") {
        const where = d.data?.current_step ? CANCEL_STEP[d.data.current_step] ?? d.data.current_step : null;
        setCancelInfo(d.data?.error_message ? `Meta reportó: ${d.data.error_message}` : `Cerraste la ventana de Meta${where ? ` ${where}` : ""}. Puedes retomarlo cuando quieras.`);
      } else if (d.event === "ERROR") {
        setCancelInfo(`Meta reportó un error: ${d.data?.error_message ?? "desconocido"}`);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  async function waitForSession(ms = 4000): Promise<SessionInfo> {
    const start = Date.now();
    while (Date.now() - start < ms) {
      if (session.current.phone_number_id && session.current.waba_id) return session.current;
      await new Promise((r) => setTimeout(r, 200));
    }
    return session.current;
  }

  async function start() {
    if (!cfg.enabled || !cfg.app_id) return;
    setError(null);
    setCancelInfo(null);
    session.current = {};
    setPhase("meta");
    let fb: FB;
    try {
      fb = await loadSdk(cfg.app_id, cfg.api_version);
    } catch (e) {
      setPhase("idle");
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    const extras: Record<string, unknown> = { setup: {}, sessionInfoVersion: "3" };
    if (coexistence) extras.featureType = "whatsapp_business_app_onboarding";
    fb.login(
      (r) => {
        const code = r.authResponse?.code;
        if (!code) {
          setPhase("idle");
          setCancelInfo((prev) => prev ?? "Se canceló la conexión con Meta.");
          return;
        }
        void (async () => {
          const info = await waitForSession();
          if (!info.phone_number_id || !info.waba_id) {
            setPhase("idle");
            setError("Meta no informó el número elegido. Vuelve a intentarlo y completa todos los pasos de la ventana.");
            return;
          }
          setPhase("linking");
          const res = await run(() =>
            send<Connected>("/api/onboarding/whatsapp", "POST", {
              code,
              waba_id: info.waba_id,
              phone_number_id: info.phone_number_id,
              coexistence,
              name: answers.company?.name ?? state.org.name,
            }),
          );
          setPhase("idle");
          if (res) {
            setResult(res);
            await reload();
          }
        })();
      },
      { config_id: cfg.config_id, response_type: "code", override_default_response_type: true, extras },
    );
  }

  async function saveManual() {
    const ok = await run(async () => {
      await send("/api/channels", "POST", {
        name: manual.name || answers.company?.name || "WhatsApp",
        phone_number_id: manual.phone_number_id.trim(),
        waba_id: manual.waba_id.trim() || null,
        display_phone: manual.display_phone.trim() || null,
        access_token: manual.access_token.trim() || null,
        bot_id: null,
      });
      // El asistente toma el número recién creado
      await send("/api/onboarding/steps/whatsapp/run", "POST");
      return true;
    });
    if (ok) await reload();
  }

  const connected = result?.channel ?? (existing ? { id: existing.id, name: existing.name, display_phone: existing.display_phone, phone_number_id: existing.phone_number_id ?? "", waba_id: existing.waba_id ?? "", verified_name: null } : null);

  return (
    <div className="ob-step">
      {connected ? (
        <section className="ob-success-card" aria-live="polite">
          <div className="ob-success-icon" aria-hidden>
            ✓
          </div>
          <div>
            <h3>Número conectado</h3>
            <p>
              <span className="strong">{connected.verified_name || connected.name}</span>
              {connected.display_phone && <> · {connected.display_phone}</>}
            </p>
            <p className="small muted">
              Phone number ID {connected.phone_number_id} · WABA {connected.waba_id}
            </p>
          </div>
        </section>
      ) : (
        <section className="ob-hero-card">
          <h3>Conecta tu número de WhatsApp</h3>
          <p className="muted small">
            Se abre una ventana de Meta: inicias sesión con Facebook, eliges (o creas) tu portafolio de negocio y la cuenta de WhatsApp Business, y verificas el número con un código por SMS o llamada. Nosotros
            registramos el número, activamos la recepción de mensajes y guardamos las credenciales de forma segura.
          </p>
          <Toggle
            checked={coexistence}
            onChange={(v) => update((a) => ({ ...a, whatsapp: { ...(a.whatsapp ?? {}), coexistence: v } }))}
            label="Ya uso este número en la app WhatsApp Business (coexistencia)"
          />
          {coexistence && (
            <p className="small muted ob-note">
              Con coexistencia sigues usando la app en tu celular y además atiendes desde aquí. Meta sincroniza tus contactos y hasta 6 meses de historial. Escanearás un código QR desde la app durante el proceso.
            </p>
          )}
          {cfg.enabled ? (
            <button className="primary ob-meta-btn" onClick={start} disabled={phase !== "idle" || busy}>
              {phase === "meta" ? "Completa los pasos en la ventana de Meta…" : phase === "linking" ? "Registrando tu número…" : "Conectar con Meta"}
            </button>
          ) : (
            <p className="ob-note warn small">
              La conexión automática con Meta no está habilitada en este servidor. El administrador de la plataforma debe configurar <code>META_APP_ID</code>, <code>META_APP_SECRET</code> y{" "}
              <code>META_EMBEDDED_SIGNUP_CONFIG_ID</code>. Mientras tanto puedes conectar el número manualmente.
            </p>
          )}
          {cancelInfo && <p className="small ob-note warn">{cancelInfo}</p>}
          <ErrorBox error={error} />
        </section>
      )}

      {result?.pin && (
        <section className="ob-pin-card" role="alert">
          <h3>Guarda este PIN</h3>
          <p className="small">Es la verificación en dos pasos del número. Meta lo pedirá si alguna vez vuelves a registrarlo. No lo volveremos a mostrar.</p>
          <div className="inline">
            <code className="ob-pin">{result.pin}</code>
            <button onClick={() => copy(result.pin!)}>Copiar</button>
          </div>
        </section>
      )}

      {!connected && (
        <details className="card ob-manual" open={!cfg.enabled}>
          <summary className="strong">Conectar manualmente (avanzado)</summary>
          <p className="small muted">Si ya tienes el número en la API de WhatsApp Cloud: copia los datos desde Meta for Developers → WhatsApp → Configuración de la API. El token debe ser de un usuario del sistema (permanente).</p>
          <div className="grid2">
            <Field label="Nombre">
              <input value={manual.name} onChange={(e) => setManual({ ...manual, name: e.target.value })} placeholder={answers.company?.name ?? "WhatsApp"} />
            </Field>
            <Field label="Número visible">
              <input value={manual.display_phone} onChange={(e) => setManual({ ...manual, display_phone: e.target.value })} placeholder="+57 300 000 0000" />
            </Field>
            <Field label="Phone number ID">
              <input value={manual.phone_number_id} onChange={(e) => setManual({ ...manual, phone_number_id: e.target.value })} inputMode="numeric" />
            </Field>
            <Field label="WhatsApp Business Account ID (WABA)">
              <input value={manual.waba_id} onChange={(e) => setManual({ ...manual, waba_id: e.target.value })} inputMode="numeric" />
            </Field>
          </div>
          <Field label="Token de acceso permanente">
            <input type="password" autoComplete="off" value={manual.access_token} onChange={(e) => setManual({ ...manual, access_token: e.target.value })} />
          </Field>
          <div className="inline" style={{ justifyContent: "flex-end" }}>
            <button className="primary" onClick={saveManual} disabled={busy || !manual.phone_number_id.trim() || !manual.access_token.trim()}>
              {busy ? "Conectando…" : "Conectar número"}
            </button>
          </div>
        </details>
      )}

      <StepFooter onBack={() => goTo("company")} onNext={next} nextDisabled={!connected} nextLabel={connected ? "Validar el número" : "Continuar"} />
    </div>
  );
}
