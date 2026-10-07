"use client";

import { useEffect, useRef, useState } from "react";
import { api, send } from "@/lib/api";
import type { EmbeddedSignupConfig } from "@/lib/saas-types";
import { ErrorBox, Field, Modal } from "@/components/ui";

type FB = {
  init: (o: Record<string, unknown>) => void;
  login: (cb: (r: { authResponse?: { code?: string } }) => void, o: Record<string, unknown>) => void;
};
declare global {
  interface Window {
    FB?: FB;
    fbAsyncInit?: () => void;
  }
}

function loadSdk(appId: string, version: string): Promise<FB> {
  return new Promise((resolve, reject) => {
    if (window.FB) return resolve(window.FB);
    window.fbAsyncInit = () => {
      window.FB!.init({ appId, autoLogAppEvents: true, xfbml: false, version });
      resolve(window.FB!);
    };
    const s = document.createElement("script");
    s.src = "https://connect.facebook.net/es_LA/sdk.js";
    s.async = true;
    s.crossOrigin = "anonymous";
    s.onerror = () => reject(new Error("No se pudo cargar el SDK de Facebook"));
    document.body.appendChild(s);
  });
}

/** «Conectar WhatsApp» con Meta Embedded Signup: el cliente autoriza su número y el servidor lo registra. */
export function EmbeddedSignupButton({ onConnected }: { onConnected?: () => void }) {
  const [cfg, setCfg] = useState<EmbeddedSignupConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ code: string; phone_number_id: string; waba_id: string } | null>(null);
  const [pin, setPin] = useState("");
  const session = useRef<{ phone_number_id?: string; waba_id?: string }>({});

  useEffect(() => {
    api<EmbeddedSignupConfig>("/api/channels/embedded-signup/config").then(setCfg, () => setCfg(null));
    const onMessage = (e: MessageEvent) => {
      if (!e.origin.endsWith("facebook.com")) return;
      try {
        const d = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
        if (d?.type === "WA_EMBEDDED_SIGNUP" && d.data?.phone_number_id) session.current = d.data;
      } catch {}
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  if (!cfg) return null;

  async function start() {
    if (!cfg?.enabled || !cfg.app_id) return;
    setError(null);
    try {
      const fb = await loadSdk(cfg.app_id, cfg.api_version);
      fb.login(
        (r) => {
          const code = r.authResponse?.code;
          const { phone_number_id, waba_id } = session.current;
          if (!code) return setError("Se canceló la conexión con Meta.");
          if (!phone_number_id || !waba_id) return setError("Meta no informó el número seleccionado; inténtalo de nuevo.");
          setPending({ code, phone_number_id, waba_id });
        },
        {
          config_id: cfg.config_id,
          response_type: "code",
          override_default_response_type: true,
          extras: { setup: {}, featureType: "", sessionInfoVersion: "3" },
        },
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function finish() {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      await send("/api/channels/embedded-signup", "POST", { ...pending, pin: pin || null });
      setPending(null);
      setPin("");
      onConnected?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        onClick={start}
        disabled={!cfg.enabled}
        title={cfg.enabled ? "Conecta tu número con Meta en pocos pasos" : "Falta configurar META_APP_ID, META_APP_SECRET y META_EMBEDDED_SIGNUP_CONFIG_ID en el servidor"}
      >
        Conectar WhatsApp
      </button>
      {error && !pending && <ErrorBox error={error} />}
      {pending && (
        <Modal
          title="Finalizar conexión"
          onClose={() => setPending(null)}
          footer={
            <button className="primary" onClick={finish} disabled={busy}>
              {busy ? "Conectando…" : "Conectar número"}
            </button>
          }
        >
          <p className="muted small">Número autorizado en Meta (ID {pending.phone_number_id}).</p>
          <Field label="PIN de verificación en dos pasos" hint="Solo si el número aún no está registrado en la API de WhatsApp (6 dígitos).">
            <input value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" />
          </Field>
          <ErrorBox error={error} />
        </Modal>
      )}
    </>
  );
}
