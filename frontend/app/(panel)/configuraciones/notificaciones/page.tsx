"use client";

import { useEffect, useState } from "react";
import { api, send } from "@/lib/api";
import {
  PREFERENCE_LABELS,
  currentSubscription,
  disablePush,
  enablePush,
  pushSupported,
  savePreferences,
  type PushPreferences,
} from "@/lib/push";
import { Card, ErrorBox, PageHeader, Toggle, useAction } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";

const DEFAULTS: PushPreferences = { assigned: true, message_assigned: true, call: true, new_conversation: false };

/** Avisos push de este navegador para el asesor conectado (y cómo instalar la app). */
export default function NotificationsPage() {
  const [supported, setSupported] = useState<boolean | null>(null);
  const [active, setActive] = useState(false);
  const [prefs, setPrefs] = useState<PushPreferences>(DEFAULTS);
  const [installed, setInstalled] = useState(false);
  const [sent, setSent] = useState<number | null>(null);
  const [run, busy, error] = useAction();

  useEffect(() => {
    setSupported(pushSupported());
    setInstalled(window.matchMedia?.("(display-mode: standalone)").matches ?? false);
    (async () => {
      const sub = await currentSubscription();
      if (!sub) return;
      setActive(true);
      const mine = await fetchMine(sub.endpoint);
      if (mine) setPrefs({ ...DEFAULTS, ...mine });
    })();
  }, []);

  const toggle = async (on: boolean) => {
    if (on) {
      const p = await run(() => enablePush(prefs));
      if (p) {
        setPrefs(p);
        setActive(true);
      }
    } else if (await run(() => disablePush().then(() => true))) {
      setActive(false);
    }
  };
  const change = async (key: keyof PushPreferences, value: boolean) => {
    const next = { ...prefs, [key]: value };
    setPrefs(next);
    if (active) {
      const saved = await run(() => savePreferences(next));
      if (saved) setPrefs(saved);
    }
  };
  const test = async () => {
    const r = await run(() => send<{ sent: number }>("/api/push/test", "POST"));
    if (r) setSent(r.sent);
  };

  return (
    <>
      <PageHeader title="Configuraciones" subtitle="Notificaciones de este navegador y app instalable para asesores." />
      <ConfigTabs />
      <Card title="Notificaciones push">
        {supported === false ? (
          <p className="small">
            Este navegador no admite notificaciones push. En iPhone o iPad, primero instala la app («Compartir → Agregar a
            inicio») y ábrela desde el ícono.
          </p>
        ) : (
          <div className="stack">
            <Toggle checked={active} onChange={(v) => !busy && toggle(v)} label={active ? "Activadas en este navegador" : "Desactivadas en este navegador"} />
            <div className="stack" style={{ gap: 6 }}>
              <span className="small strong">Avisarme cuando…</span>
              {(Object.keys(PREFERENCE_LABELS) as (keyof PushPreferences)[]).map((k) => (
                <label key={k} className="inline small">
                  <input type="checkbox" checked={prefs[k]} disabled={busy} onChange={(e) => change(k, e.target.checked)} />
                  {PREFERENCE_LABELS[k]}
                </label>
              ))}
            </div>
            {active && (
              <div className="inline">
                <button onClick={test} disabled={busy}>Enviar aviso de prueba</button>
                {sent !== null && <span className="small muted">{sent ? "Enviado: revisa tus notificaciones." : "No se envió a ningún dispositivo."}</span>}
              </div>
            )}
            {error && <ErrorBox error={error} />}
          </div>
        )}
      </Card>
      <Card title="Instalar la app">
        {installed ? (
          <p className="small">Estás usando la app instalada.</p>
        ) : (
          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
            <li>Chrome o Edge (computador o Android): menú ⋮ → «Instalar app» o el ícono de instalar en la barra de direcciones.</li>
            <li>iPhone / iPad (Safari): botón Compartir → «Agregar a inicio». Las notificaciones funcionan desde iOS 16.4 abriendo la app desde el ícono.</li>
            <li>La app abre directo en Conversaciones y avisa aunque el panel esté cerrado.</li>
          </ul>
        )}
      </Card>
    </>
  );
}

async function fetchMine(endpoint: string): Promise<Partial<PushPreferences> | null> {
  try {
    const subs = await api<{ endpoint: string; preferences: Partial<PushPreferences> }[]>("/api/push/subscriptions");
    return subs.find((s) => s.endpoint === endpoint)?.preferences ?? null;
  } catch {
    return null;
  }
}
