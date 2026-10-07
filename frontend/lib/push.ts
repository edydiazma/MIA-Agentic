/** Web Push del asesor: suscribe este navegador con la llave VAPID del servidor (app instalable). */
import { api, send } from "@/lib/api";

export type PushPreferences = {
  assigned: boolean;
  message_assigned: boolean;
  call: boolean;
  new_conversation: boolean;
};

export const PREFERENCE_LABELS: Record<keyof PushPreferences, string> = {
  assigned: "Me asignan una conversación",
  message_assigned: "Mensaje nuevo en mis conversaciones (si no tengo el panel abierto)",
  call: "Llamada entrante de WhatsApp",
  new_conversation: "Conversación nueva en mis grupos sin asesor",
};

export const pushSupported = () =>
  typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    return null;
  }
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

/** Pide permiso, suscribe el navegador y la guarda en el servidor. */
export async function enablePush(preferences?: Partial<PushPreferences>): Promise<PushPreferences> {
  if (!pushSupported()) throw new Error("Este navegador no admite notificaciones push");
  const { enabled, public_key } = await api<{ enabled: boolean; public_key: string | null }>("/api/push/vapid-public-key");
  if (!enabled || !public_key) throw new Error("El servidor no tiene configuradas las notificaciones (VAPID)");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Permiso de notificaciones denegado en el navegador");
  const reg = (await navigator.serviceWorker.getRegistration("/")) ?? (await registerServiceWorker());
  if (!reg) throw new Error("No se pudo registrar el service worker");
  await navigator.serviceWorker.ready;
  const sub =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(public_key) }));
  const json = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
  const res = await send<{ preferences: PushPreferences }>(
    `/api/push/subscriptions?user_agent=${encodeURIComponent(navigator.userAgent.slice(0, 300))}`,
    "POST",
    { endpoint: json.endpoint, keys: json.keys, preferences },
  );
  return res.preferences;
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await send("/api/push/subscriptions", "DELETE", { endpoint: sub.endpoint }).catch(() => undefined);
  await sub.unsubscribe();
}

export async function savePreferences(preferences: Partial<PushPreferences>): Promise<PushPreferences> {
  const sub = await currentSubscription();
  if (!sub) throw new Error("Activa primero las notificaciones en este navegador");
  const res = await send<{ preferences: PushPreferences }>("/api/push/subscriptions/preferences", "PUT", {
    endpoint: sub.endpoint,
    preferences,
  });
  return res.preferences;
}
