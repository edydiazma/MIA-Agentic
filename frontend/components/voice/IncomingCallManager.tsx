"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, send } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { useMe } from "@/components/Shell";
import type { Call, IncomingCall } from "@/lib/voice-types";
import { fmtDuration } from "@/lib/voice-types";
import s from "./voice.module.css";

const ICE_SERVERS: RTCIceServer[] = (process.env.NEXT_PUBLIC_VOICE_STUN_URLS ?? "stun:stun.l.google.com:19302")
  .split(",")
  .map((u) => u.trim())
  .filter(Boolean)
  .map((urls) => ({ urls }));
const DONE = new Set(["ended", "missed", "rejected", "failed"]);

type Active = { call: Call; pc: RTCPeerConnection; mic: MediaStream; startedAt: number };

/** Espera a que termine la recolección de candidatos ICE (Meta no usa trickle ICE: todo va en el SDP). */
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

/** Timbre generado con WebAudio (tono doble 440/480 Hz, 1 s cada 3 s). */
function useRingtone(on: boolean) {
  useEffect(() => {
    if (!on || typeof window === "undefined") return;
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    ctx.resume().catch(() => {});
    const burst = () => {
      const gain = ctx.createGain();
      gain.gain.value = 0.08;
      gain.connect(ctx.destination);
      for (const f of [440, 480]) {
        const o = ctx.createOscillator();
        o.frequency.value = f;
        o.connect(gain);
        o.start();
        o.stop(ctx.currentTime + 1);
      }
    };
    burst();
    const id = setInterval(burst, 3000);
    return () => {
      clearInterval(id);
      ctx.close().catch(() => {});
    };
  }, [on]);
}

const label = (c: Call) => c.contact_name || (c.wa_id ? `+${c.wa_id}` : "Cliente");

/** Llamadas entrantes de WhatsApp: aviso con timbre, contestar en el navegador (WebRTC) y barra de llamada en curso. */
export function IncomingCallManager() {
  const me = useMe();
  const [ringing, setRinging] = useState<IncomingCall[]>([]);
  const [active, setActive] = useState<Active | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [muted, setMuted] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [notice, setNotice] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const activeRef = useRef<Active | null>(null);
  activeRef.current = active;

  useRingtone(ringing.length > 0 && !active);

  const flash = useCallback((msg: string) => {
    setNotice(msg);
    setTimeout(() => setNotice(null), 5000);
  }, []);

  const cleanup = useCallback(() => {
    const a = activeRef.current;
    if (a) {
      a.pc.close();
      a.mic.getTracks().forEach((t) => t.stop());
    }
    if (audioRef.current) audioRef.current.srcObject = null;
    setActive(null);
    setMuted(false);
  }, []);

  // Al recargar la página: llamadas que siguen sonando
  useEffect(() => {
    if (!me || me.availability !== "available") return;
    api<IncomingCall[]>("/api/calls/active")
      .then((rows) => setRinging(rows.filter((c) => (c.status === "ringing" && c.sdp_offer) || c.status === "transferring")))
      .catch(() => {});
  }, [me]);

  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);

  useEffect(() => cleanup, [cleanup]);

  useRealtime((event, data) => {
    if (!me || !event.startsWith("call.")) return;
    if (event === "call.incoming") {
      const c = data as IncomingCall;
      if (c.targets && !c.targets.includes(me.id)) return;
      if (c.mode === "answer" && !c.sdp_offer) return;
      setRinging((rs) => [...rs.filter((r) => r.id !== c.id), c]);
      return;
    }
    if (event === "call.taken" || event === "call.updated") {
      const c = data as Call;
      const stillRinging = c.status === "ringing" || c.status === "transferring";
      if (event === "call.taken" || !stillRinging) setRinging((rs) => rs.filter((r) => r.id !== c.id));
      const a = activeRef.current;
      if (a && a.call.id === c.id) {
        if (DONE.has(c.status)) {
          cleanup();
          flash(`Llamada finalizada · ${fmtDuration(c.duration_s)}`);
        } else setActive({ ...a, call: c });
      }
    }
  });

  async function answer(c: IncomingCall) {
    if (active || busy) return;
    setBusy(c.id);
    let pc: RTCPeerConnection | null = null;
    let mic: MediaStream | null = null;
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
      mic.getAudioTracks().forEach((t) => pc!.addTrack(t, mic!));
      pc.ontrack = (e) => {
        if (audioRef.current) {
          audioRef.current.srcObject = e.streams[0] ?? new MediaStream([e.track]);
          audioRef.current.play().catch(() => {});
        }
      };
      let call: Call;
      if (c.mode === "bridge") {
        // Transferencia desde el agente de voz: el servidor puentea el audio del cliente
        await pc.setLocalDescription(await pc.createOffer());
        await iceComplete(pc);
        const r = await send<{ sdp: string; call: Call }>(`/api/calls/${c.id}/bridge`, "POST", { sdp: pc.localDescription!.sdp });
        await pc.setRemoteDescription({ type: "answer", sdp: r.sdp });
        call = r.call;
      } else {
        // Contestar directo la oferta SDP de Meta
        await pc.setRemoteDescription({ type: "offer", sdp: c.sdp_offer! });
        await pc.setLocalDescription(await pc.createAnswer());
        await iceComplete(pc);
        call = await send<Call>(`/api/calls/${c.id}/answer`, "POST", { sdp: pc.localDescription!.sdp });
      }
      setRinging((rs) => rs.filter((r) => r.id !== c.id));
      setActive({ call, pc, mic, startedAt: Date.now() });
      setNow(Date.now());
    } catch (e) {
      pc?.close();
      mic?.getTracks().forEach((t) => t.stop());
      if (e instanceof ApiError && e.status === 409) {
        setRinging((rs) => rs.filter((r) => r.id !== c.id));
        flash(e.message);
      } else if (e instanceof DOMException && e.name === "NotAllowedError") {
        flash("Permite el acceso al micrófono para contestar llamadas.");
      } else flash(`No se pudo contestar: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  async function decline(c: IncomingCall) {
    setRinging((rs) => rs.filter((r) => r.id !== c.id));
    // Si solo me sonaba a mí, se rechaza en WhatsApp; si no, sigue sonando para los demás asesores
    if (c.mode === "answer" && (c.targets?.length ?? 1) <= 1) await send(`/api/calls/${c.id}/reject`, "POST").catch(() => {});
  }

  async function hangup() {
    const a = activeRef.current;
    if (!a) return;
    await send(`/api/calls/${a.call.id}/hangup`, "POST").catch(() => {});
    cleanup();
  }

  function toggleMute() {
    const a = activeRef.current;
    if (!a) return;
    const next = !muted;
    a.mic.getAudioTracks().forEach((t) => (t.enabled = !next));
    setMuted(next);
  }

  if (!me) return null;
  return (
    <>
      <audio ref={audioRef} autoPlay hidden />
      {(ringing.length > 0 || active || notice) && (
        <div className={s.dock} aria-live="polite">
          {notice && <div className={s.notice}>{notice}</div>}
          {!active &&
            ringing.map((c) => (
              <div key={c.id} className={`${s.toast} ${c.mode === "bridge" ? s.bridge : ""}`} role="alertdialog" aria-label="Llamada entrante">
                <div className={s.head}>
                  <span className={s.pulse} aria-hidden>
                    📞
                  </span>
                  <div className={s.who}>
                    <strong>{label(c)}</strong>
                    <small className="muted">
                      {c.mode === "bridge" ? "El agente de voz transfiere la llamada" : "Llamada de WhatsApp entrante"}
                      {c.wa_id && c.contact_name ? ` · +${c.wa_id}` : ""}
                    </small>
                  </div>
                </div>
                <div className={s.actions}>
                  <button className={s.decline} onClick={() => decline(c)} disabled={busy === c.id}>
                    {c.mode === "bridge" ? "Ignorar" : "Rechazar"}
                  </button>
                  <button className={s.answer} onClick={() => answer(c)} disabled={busy !== null}>
                    {busy === c.id ? "Conectando…" : "Contestar"}
                  </button>
                </div>
              </div>
            ))}
          {active && (
            <div className={s.bar} role="region" aria-label="Llamada en curso">
              <span aria-hidden>🎧</span>
              <div className={s.who}>
                <strong>{label(active.call)}</strong>
                <small>{ringing.length ? `${ringing.length} en espera` : "En llamada"}</small>
              </div>
              <span className={s.timer}>{fmtDuration(Math.max(0, Math.floor((now - active.startedAt) / 1000)))}</span>
              <button className={s.barBtn} onClick={toggleMute} aria-pressed={muted} title={muted ? "Activar micrófono" : "Silenciar"}>
                {muted ? "🔇" : "🎙️"}
              </button>
              <button className={s.hang} onClick={hangup}>
                Colgar
              </button>
            </div>
          )}
        </div>
      )}
    </>
  );
}
