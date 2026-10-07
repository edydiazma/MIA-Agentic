"use client";

import { createContext, useContext, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  AVAILABILITY_LABEL,
  getAgent,
  getToken,
  send,
  setSession,
  type Agent,
  type Availability,
} from "@/lib/api";
import { NAV, type NavEntry } from "@/lib/nav";
import { RealtimeProvider, useRealtime, useRealtimeStatus } from "@/lib/realtime";

const MeContext = createContext<Agent | null>(null);
/** Usuario que inició sesión (role, availability...). */
export const useMe = () => useContext(MeContext);

function isActive(path: string, href: string) {
  return href === "/" ? path === "/" : path === href || path.startsWith(href + "/");
}

function Section({ entry, path, isAdmin, onNavigate }: { entry: NavEntry; path: string; isAdmin: boolean; onNavigate: () => void }) {
  const childActive = entry.groups?.some((g) => g.items.some((i) => isActive(path, i.href))) ?? false;
  const [open, setOpen] = useState(childActive);
  useEffect(() => {
    if (childActive) setOpen(true);
  }, [childActive]);

  if (entry.href)
    return (
      <Link href={entry.href} className={isActive(path, entry.href) ? "nav-link active" : "nav-link"} onClick={onNavigate}>
        <span className="nav-icon">{entry.icon}</span>
        {entry.label}
      </Link>
    );

  return (
    <div className="nav-section">
      <button className={childActive ? "nav-link active-parent" : "nav-link"} onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="nav-icon">{entry.icon}</span>
        {entry.label}
        <span className="chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="nav-sub">
          {entry.groups!.map((g) => (
            <div key={g.label || entry.label}>
              {g.label && <div className="nav-group-label">{g.label}</div>}
              {g.items
                .filter((i) => isAdmin || !i.adminOnly)
                .map((i) => (
                  <Link
                    key={i.href}
                    href={i.href}
                    className={isActive(path, i.href) ? "nav-sublink active" : "nav-sublink"}
                    onClick={onNavigate}
                  >
                    {i.label}
                    {i.soon && <span className="soon-dot" title="Próxima fase" />}
                  </Link>
                ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Shell({ me, setMe, children }: { me: Agent; setMe: (a: Agent) => void; children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const connected = useRealtimeStatus();
  const [menuOpen, setMenuOpen] = useState(false);
  const [alerts, setAlerts] = useState(0);
  const isAdmin = me.role === "admin";

  useRealtime((event) => {
    if (event === "alert.new") setAlerts((n) => n + 1);
  });
  useEffect(() => {
    if (path === "/") setAlerts(0);
  }, [path]);

  async function changeAvailability(v: Availability) {
    const updated = await send<Agent>("/api/auth/me/availability", "PUT", { availability: v });
    setSession(getToken(), updated);
    setMe(updated);
  }

  function logout() {
    setSession(null);
    router.replace("/login");
  }

  return (
    <div className="shell">
      <aside className={`sidebar ${menuOpen ? "open" : ""}`}>
        <div className="brand">
          <span className="brand-mark">◆</span> WA Agent
        </div>
        <nav className="nav">
          {NAV.filter((e) => isAdmin || !e.adminOnly).map((e) => (
            <Section key={e.label} entry={e} path={path} isAdmin={isAdmin} onNavigate={() => setMenuOpen(false)} />
          ))}
        </nav>
      </aside>
      {menuOpen && <div className="sidebar-backdrop" onClick={() => setMenuOpen(false)} />}
      <div className="main">
        <header className="topbar">
          <button className="icon menu-btn" onClick={() => setMenuOpen(true)} aria-label="Menú">
            ☰
          </button>
          <span className={`dot ${connected ? "on" : ""}`} title={connected ? "En tiempo real" : "Reconectando…"} />
          {alerts > 0 && (
            <Link href="/" className="alert-chip">
              🔔 {alerts} {alerts === 1 ? "alerta nueva" : "alertas nuevas"}
            </Link>
          )}
          <div className="topbar-right">
            <select
              className={`availability ${me.availability}`}
              value={me.availability}
              onChange={(e) => changeAvailability(e.target.value as Availability)}
              aria-label="Disponibilidad"
            >
              {(Object.keys(AVAILABILITY_LABEL) as Availability[]).map((a) => (
                <option key={a} value={a}>
                  {AVAILABILITY_LABEL[a]}
                </option>
              ))}
            </select>
            <span className="user">
              {me.name}
              <small className="muted">{isAdmin ? "Administrador" : "Asesor"}</small>
            </span>
            <button onClick={logout}>Salir</button>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

export default function PanelShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [me, setMe] = useState<Agent | null>(null);

  useEffect(() => {
    if (!getToken()) router.replace("/login");
    else setMe(getAgent());
  }, [router]);

  if (!me) return null;
  return (
    <MeContext.Provider value={me}>
      <RealtimeProvider>
        <Shell me={me} setMe={setMe}>
          {children}
        </Shell>
      </RealtimeProvider>
    </MeContext.Provider>
  );
}
