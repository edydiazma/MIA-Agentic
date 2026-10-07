"use client";

import { useEffect } from "react";
import { getToken } from "@/lib/api";
import { currentSubscription, enablePush, registerServiceWorker } from "@/lib/push";

/** Registra el service worker (app instalable + avisos push) y renueva la suscripción si el navegador la cambió. */
export default function PwaRegister() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") return; // en desarrollo el SW estorba la recarga en caliente
    registerServiceWorker();
    const onMessage = async (e: MessageEvent) => {
      if (e.data?.type !== "push-resubscribe" || !getToken()) return;
      if (!(await currentSubscription())) await enablePush().catch(() => undefined);
    };
    navigator.serviceWorker?.addEventListener("message", onMessage);
    return () => navigator.serviceWorker?.removeEventListener("message", onMessage);
  }, []);
  return null;
}
