"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { getToken, wsUrl } from "./api";

type Handler = (event: string, data: any) => void;
type Ctx = { connected: boolean; subscribe: (h: Handler) => () => void };

const RealtimeContext = createContext<Ctx>({ connected: false, subscribe: () => () => {} });

/** Una sola conexión WebSocket por pestaña; las páginas se suscriben a los eventos. */
export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const [connected, setConnected] = useState(false);
  const handlers = useRef(new Set<Handler>());

  useEffect(() => {
    if (!getToken()) return;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout>;
    let ping: ReturnType<typeof setInterval>;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => {
        setConnected(true);
        ping = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
      };
      ws.onclose = () => {
        setConnected(false);
        clearInterval(ping);
        if (!closed) retry = setTimeout(connect, 2000);
      };
      ws.onmessage = (e) => {
        const { event, data } = JSON.parse(e.data);
        handlers.current.forEach((h) => h(event, data));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(ping);
      ws?.close();
    };
  }, []);

  const subscribe = useCallback((h: Handler) => {
    handlers.current.add(h);
    return () => {
      handlers.current.delete(h);
    };
  }, []);

  return <RealtimeContext.Provider value={{ connected, subscribe }}>{children}</RealtimeContext.Provider>;
}

/** Ejecuta `handler` con cada evento en tiempo real (siempre con la versión más reciente del handler). */
export function useRealtime(handler: Handler) {
  const { subscribe } = useContext(RealtimeContext);
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((e, d) => ref.current(e, d)), [subscribe]);
}

export const useRealtimeStatus = () => useContext(RealtimeContext).connected;
