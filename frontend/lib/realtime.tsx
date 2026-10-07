"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, getToken, wsUrl } from "./api";

type Handler = (event: string, data: any) => void;
type Ctx = { connected: boolean; transport: "ws" | "supabase" | null; subscribe: (h: Handler) => () => void };

const RealtimeContext = createContext<Ctx>({ connected: false, transport: null, subscribe: () => () => {} });

type RealtimeToken =
  | { enabled: false }
  | { enabled: true; url: string; key: string; token: string; expires_at: string; topics: { events: string; agent: string } };

/**
 * Eventos en vivo del panel (docs/data-model.md §19.2). Si el backend entrega un token de Supabase Realtime
 * (REALTIME_TRANSPORT=supabase|both), la pestaña se suscribe a los canales privados de su empresa y de su usuario
 * (`org:{id}:events`, `org:{id}:agent:{id}`) con los mismos {event, data} que el WebSocket; si no, o si Supabase
 * falla, usa el WebSocket propio (/ws). Una sola fuente a la vez para no duplicar eventos.
 */
export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [transport, setTransport] = useState<"ws" | "supabase" | null>(null);
  const handlers = useRef(new Set<Handler>());

  useEffect(() => {
    if (!getToken()) return;
    let closed = false;
    const emit = (event: string, data: any) => handlers.current.forEach((h) => h(event, data));
    let cleanup: () => void = () => {};

    // --- WebSocket propio (respaldo)
    const startWs = () => {
      let ws: WebSocket | null = null;
      let retry: ReturnType<typeof setTimeout>;
      let ping: ReturnType<typeof setInterval>;
      let stopped = false;
      const connect = () => {
        ws = new WebSocket(wsUrl());
        ws.onopen = () => {
          setConnected(true);
          ping = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
        };
        ws.onclose = () => {
          setConnected(false);
          clearInterval(ping);
          if (!stopped) retry = setTimeout(connect, 2000);
        };
        ws.onmessage = (e) => {
          const { event, data } = JSON.parse(e.data);
          emit(event, data);
        };
      };
      connect();
      setTransport("ws");
      return () => {
        stopped = true;
        clearTimeout(retry);
        clearInterval(ping);
        ws?.close();
      };
    };

    // --- Supabase Realtime
    const startSupabase = async (first: Extract<RealtimeToken, { enabled: true }>) => {
      const { createClient } = await import("@supabase/supabase-js");
      const client = createClient(first.url, first.key, {
        auth: { persistSession: false, autoRefreshToken: false },
        realtime: { params: { eventsPerSecond: 50 } },
      });
      await client.realtime.setAuth(first.token);
      let fellBack = false;
      let refresher: ReturnType<typeof setInterval> | undefined;
      const onPayload = (msg: { event: string; payload?: any }) => {
        const p = msg.payload || {};
        emit(p.event ?? msg.event, p.data ?? p);
      };
      const subscribed = new Set<string>();
      const channels: ReturnType<typeof client.channel>[] = [];
      const teardown = () => {
        if (refresher) clearInterval(refresher);
        channels.forEach((c) => client.removeChannel(c));
      };
      for (const topic of [first.topics.events, first.topics.agent]) {
        channels.push(
          client
            .channel(topic, { config: { private: true } })
            .on("broadcast", { event: "*" }, onPayload)
            .subscribe((status) => {
              if (status === "SUBSCRIBED") {
                subscribed.add(topic);
                if (subscribed.size === 2) setConnected(true);
              } else if ((status === "CHANNEL_ERROR" || status === "TIMED_OUT") && !fellBack && !closed) {
                // Política de Realtime, token o red: se vuelve al WebSocket propio
                fellBack = true;
                setConnected(false);
                teardown();
                cleanup = startWs();
              } else if (status === "CLOSED") {
                subscribed.delete(topic);
                setConnected(false);
              }
            }),
        );
      }
      setTransport("supabase");
      // Renovar el token antes de que venza
      const refreshEvery = Math.max(60_000, new Date(first.expires_at).getTime() - Date.now() - 120_000);
      refresher = setInterval(async () => {
        try {
          const next = await api<RealtimeToken>("/api/realtime/token");
          if (next.enabled) await client.realtime.setAuth(next.token);
        } catch {
          /* se reintenta en la siguiente vuelta */
        }
      }, refreshEvery);
      return teardown;
    };

    (async () => {
      let token: RealtimeToken = { enabled: false };
      try {
        token = await api<RealtimeToken>("/api/realtime/token");
      } catch {
        /* backend sin el endpoint: WebSocket */
      }
      if (closed) return;
      if (token.enabled) {
        try {
          cleanup = await startSupabase(token);
          return;
        } catch {
          /* librería o red: WebSocket */
        }
      }
      if (!closed) cleanup = startWs();
    })();

    return () => {
      closed = true;
      cleanup();
    };
  }, []);

  const subscribe = useCallback((h: Handler) => {
    handlers.current.add(h);
    return () => {
      handlers.current.delete(h);
    };
  }, []);

  return (
    <RealtimeContext.Provider value={{ connected, transport, subscribe }}>{children}</RealtimeContext.Provider>
  );
}

/** Ejecuta `handler` con cada evento en tiempo real (siempre con la versión más reciente del handler). */
export function useRealtime(handler: Handler) {
  const { subscribe } = useContext(RealtimeContext);
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((e, d) => ref.current(e, d)), [subscribe]);
}

export const useRealtimeStatus = () => useContext(RealtimeContext).connected;
export const useRealtimeTransport = () => useContext(RealtimeContext).transport;
