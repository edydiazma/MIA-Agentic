"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, send } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import type { Call } from "@/lib/voice-types";
import { fmtDuration } from "@/lib/voice-types";
import s from "./voice.module.css";

const ICE_SERVERS: RTCIceServer[] = (process.env.NEXT_PUBLIC_VOICE_STUN_URLS ?? "stun:stun.l.google.com:19302")
  .split(",")
  .map((u) => u.trim())
  .filter(Boolean)
  .map((urls) => ({ urls }));
const DONE = new Set(["ended", "missed", "rejected", "failed"]);

type Permission = {
  status: "none" | "requested" | "granted" | "denied" | "expired" | "revoked";
  can_call: boolean;
  can_request: boolean;
  permanent?: boolean;
  expires_at?: string | null;
};
type Live = { call: Call; pc: RTCPeerConnection; mic: MediaStream; startedAt: number | null };

/** Meta no usa trickle ICE: la oferta viaja con todos los candidatos. */
function iceComplete(pc: RTCPeerConnection, timeoutMs = 3000) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise<void>((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      clearTimeout(t);
      resolve();
    };
    const check = () => pc.iceGatheringState === "complete" && done();
    const t = setTimeout(done, timeoutMs);
    pc.addEventListener("icegatheringstatechange", check);
  });
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    try {
      const d = JSON.parse(e.message);
      if (d && typeof d.message === "string") return d.message;
    } catch {
      /* mensaje plano */
    }
    return e.message;
  }
  return e instanceof Error ? e.message : "No se pudo completar la acción";
}

const BADGE: Record<Permission["status"], string> = {
  none: "Sin permiso de llamada",
  requested: "Permiso solicitado",
  granted: "Puede recibir llamadas",
  denied: "No aceptó llamadas",
  expired: "Permiso vencido",
  revoked: "Permiso revocado",
};

/**
 * Llamar al cliente por WhatsApp desde la conversación (§19.1). WhatsApp exige su permiso: si no lo hay, el botón
 * lo pide (máx. 1 cada 24 h y 2 cada 7 días). Con permiso, el navegador crea la oferta WebRTC, el backend pide a
 * Meta la llamada y el SDP answer llega por el evento `call.answer` (el audio va navegador ↔ Meta).
 *
 * Montaje: en el encabezado de la conversación (components/inbox/*): <OutboundCallButton conversationId={id} />
 */
export default function OutboundCallButton({ conversationId, compact = false }: { conversationId: number; compact?: boolean }) {
  const [perm, setPerm] = useState<Permission | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [muted, setMuted] = useState(false);
  const [, tick] = useState(0);
  const liveRef = useRef<Live | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  liveRef.current = live;

  const load = useCallback(async () => {
    try {
      setPerm(await api<Permission>(`/api/conversations/${conversationId}/call-permission`));
    } catch {
      setPerm(null); // canal sin llamadas o sin acceso: no se muestra
    }
  }, [conversationId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!live?.startedAt) return;
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [live?.startedAt]);

  const cleanup = useCallback(() => {
    const l = liveRef.current;
    if (l) {
      l.pc.close();
      l.mic.getTracks().forEach((t) => t.stop());
    }
    setLive(null);
    setMuted(false);
  }, []);

  useEffect(() => cleanup, [cleanup]);

  useRealtime(async (event, data) => {
    if (event === "call.permission" && data?.conversation_id === conversationId) {
      load();
      return;
    }
    const l = liveRef.current;
    if (!l) return;
    if (event === "call.answer" && data?.call_id === l.call.id && data.sdp) {
      try {
        await l.pc.setRemoteDescription({ type: "answer", sdp: data.sdp });
      } catch (e) {
        setError(errorMessage(e));
      }
      return;
    }
    if ((event === "call.updated" || event === "call.taken") && data?.id === l.call.id) {
      const call = data as Call;
      if (DONE.has(call.status)) {
        cleanup();
        if (call.status === "rejected") setError("El cliente rechazó la llamada");
        else if (call.status === "failed") setError(call.end_reason || "La llamada falló");
        return;
      }
      setLive({ ...l, call, startedAt: call.status === "connected" ? l.startedAt ?? Date.now() : l.startedAt });
    }
  });

  async function requestPermission() {
    setBusy(true);
    setError(null);
    try {
      setPerm(await send<Permission>(`/api/conversations/${conversationId}/call-permission`, "POST", {}));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function startCall() {
    setBusy(true);
    setError(null);
    let mic: MediaStream | null = null;
    let pc: RTCPeerConnection | null = null;
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
      mic.getTracks().forEach((t) => pc!.addTrack(t, mic!));
      pc.ontrack = (ev) => {
        if (audioRef.current) audioRef.current.srcObject = ev.streams[0];
      };
      await pc.setLocalDescription(await pc.createOffer());
      await iceComplete(pc);
      const call = await send<Call>(`/api/conversations/${conversationId}/call`, "POST", {
        sdp: pc.localDescription?.sdp ?? "",
      });
      setLive({ call, pc, mic, startedAt: null });
    } catch (e) {
      pc?.close();
      mic?.getTracks().forEach((t) => t.stop());
      setError(
        e instanceof DOMException && e.name === "NotAllowedError"
          ? "Permite el micrófono en el navegador para llamar"
          : errorMessage(e),
      );
    } finally {
      setBusy(false);
    }
  }

  async function hangup() {
    const l = liveRef.current;
    if (!l) return;
    try {
      await send(`/api/calls/${l.call.id}/hangup`, "POST");
    } catch {
      /* el estado final llega por tiempo real */
    }
    cleanup();
  }

  function toggleMute() {
    const l = liveRef.current;
    if (!l) return;
    const next = !muted;
    l.mic.getAudioTracks().forEach((t) => (t.enabled = !next));
    setMuted(next);
  }

  if (!perm) return null;

  if (live) {
    const connected = live.call.status === "connected";
    const secs = connected && live.startedAt ? Math.floor((Date.now() - live.startedAt) / 1000) : 0;
    return (
      <div className={s.bar} role="status" aria-live="polite">
        <audio ref={audioRef} autoPlay />
        <span className={s.who}>
          <strong>{live.call.contact_name || live.call.wa_id || "Cliente"}</strong>
          <small>{connected ? "En llamada" : "Llamando…"}</small>
        </span>
        <span className={s.timer}>{connected ? fmtDuration(secs) : ""}</span>
        <button className={s.barBtn} aria-pressed={muted} onClick={toggleMute}>
          {muted ? "Activar micrófono" : "Silenciar"}
        </button>
        <button className={s.hang} onClick={hangup}>
          Colgar
        </button>
      </div>
    );
  }

  return (
    <span className="inline" style={{ gap: 6 }}>
      <audio ref={audioRef} autoPlay hidden />
      {perm.can_call ? (
        <button onClick={startCall} disabled={busy} title="Llamar por WhatsApp">
          📞 {compact ? "" : "Llamar"}
        </button>
      ) : (
        <button
          onClick={requestPermission}
          disabled={busy || !perm.can_request}
          title={perm.can_request ? "WhatsApp exige que el cliente acepte las llamadas" : "Límite de solicitudes alcanzado"}
        >
          📞 {compact ? "" : "Solicitar permiso de llamada"}
        </button>
      )}
      {!compact && (
        <small className="muted" title={perm.expires_at ? `Vence: ${new Date(perm.expires_at).toLocaleString("es")}` : undefined}>
          {BADGE[perm.status] ?? perm.status}
          {perm.permanent ? " (permanente)" : ""}
        </small>
      )}
      {error && (
        <small className="error" role="alert">
          {error}
        </small>
      )}
    </span>
  );
}
