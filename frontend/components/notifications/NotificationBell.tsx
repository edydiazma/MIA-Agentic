"use client";

// Campana de notificaciones del panel. Se monta en el Shell (barra superior).
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, send, timeAgo } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import {
  NOTIFICATION_ICONS,
  type AppNotification,
  type NotificationPage,
  type NotificationPrefs,
} from "@/lib/productivity-types";

function beep() {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
    osc.onended = () => ctx.close();
  } catch {
    /* sin audio: no pasa nada */
  }
}

export default function NotificationBell() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [cursor, setCursor] = useState<number | null>(null);
  const [prefs, setPrefs] = useState<NotificationPrefs | null>(null);
  const [loading, setLoading] = useState(false);
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">("default");
  const box = useRef<HTMLDivElement>(null);

  const load = useCallback(async (more = false) => {
    setLoading(true);
    try {
      const page = await api<NotificationPage>(`/api/notifications?limit=20${more && cursor ? `&cursor=${cursor}` : ""}`);
      setItems((prev) => (more ? [...prev, ...page.items] : page.items));
      setCursor(page.next_cursor);
      setUnread(page.unread);
    } catch {
      /* sin conexión: se reintenta al abrir */
    } finally {
      setLoading(false);
    }
  }, [cursor]);

  useEffect(() => {
    load();
    api<NotificationPrefs>("/api/me/notification-prefs").then(setPrefs).catch(() => {});
    setPermission(typeof window !== "undefined" && "Notification" in window ? Notification.permission : "unsupported");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  useRealtime((event, data) => {
    if (event !== "notification.new") return;
    const n = data as unknown as AppNotification;
    setItems((prev) => [n, ...prev.filter((x) => x.id !== n.id)]);
    setUnread((u) => u + 1);
    if (prefs?.sound !== false) beep();
    if (prefs?.desktop !== false && permission === "granted" && document.visibilityState !== "visible") {
      try {
        const desk = new Notification(n.title, { body: n.body ?? "", tag: `n-${n.id}` });
        desk.onclick = () => {
          window.focus();
          if (n.link) router.push(n.link);
        };
      } catch {
        /* algunos navegadores solo permiten avisos desde el service worker */
      }
    }
  });

  async function markRead(ids: number[] | "all") {
    const r = await send<{ unread: number }>("/api/notifications/read", "POST", ids === "all" ? { all: true } : { ids });
    setUnread(r.unread);
    setItems((prev) => prev.map((n) => (ids === "all" || ids.includes(n.id) ? { ...n, read: true } : n)));
  }

  async function openItem(n: AppNotification) {
    if (!n.read) await markRead([n.id]).catch(() => {});
    setOpen(false);
    if (n.link) router.push(n.link);
  }

  async function toggleSound() {
    if (!prefs) return;
    setPrefs(await send<NotificationPrefs>("/api/me/notification-prefs", "PUT", { sound: !prefs.sound }));
  }

  async function askDesktop() {
    if (!("Notification" in window)) return;
    setPermission(await Notification.requestPermission());
  }

  return (
    <div className="nb" ref={box}>
      <button
        className="icon nb-btn"
        aria-label={unread ? `Notificaciones: ${unread} sin leer` : "Notificaciones"}
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
          if (!open) load();
        }}
      >
        🔔{unread > 0 && <span className="nb-badge">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="nb-panel" role="dialog" aria-label="Notificaciones">
          <div className="nb-head">
            <strong>Notificaciones</strong>
            <div className="inline">
              <button className="link small" onClick={toggleSound} title="Sonido de las notificaciones">
                {prefs?.sound === false ? "🔇 Sonido" : "🔊 Sonido"}
              </button>
              {unread > 0 && (
                <button className="link small" onClick={() => markRead("all")}>
                  Marcar todo como leído
                </button>
              )}
            </div>
          </div>
          {permission === "default" && (
            <div className="nb-perm small">
              Activa los avisos del escritorio para enterarte aunque el panel esté en otra pestaña.{" "}
              <button className="link small" onClick={askDesktop}>
                Activar
              </button>
            </div>
          )}
          <ul className="nb-list">
            {items.length === 0 && !loading && <li className="muted small nb-empty">No tienes notificaciones</li>}
            {items.map((n) => (
              <li key={n.id}>
                <button className={`nb-item ${n.read ? "" : "unread"}`} onClick={() => openItem(n)}>
                  <span className="nb-icon" aria-hidden>
                    {NOTIFICATION_ICONS[n.type] ?? "•"}
                  </span>
                  <span className="nb-text">
                    <span className="strong">{n.title}</span>
                    {n.body && <span className="muted small nb-body">{n.body}</span>}
                    <span className="muted small">{timeAgo(n.created_at)}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {cursor && (
            <button className="link small nb-more" disabled={loading} onClick={() => load(true)}>
              Ver anteriores
            </button>
          )}
        </div>
      )}
    </div>
  );
}
