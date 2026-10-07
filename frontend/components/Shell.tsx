"use client";

import { createContext, useContext, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  api,
  getAgent,
  getToken,
  send,
  setSession,
  type Agent,
} from "@/lib/api";
import { NAV, type NavEntry } from "@/lib/nav";
import { RealtimeProvider, useRealtime, useRealtimeStatus } from "@/lib/realtime";
import type { PlanStatus } from "@/lib/saas-types";
import { ONBOARDING_SKIP_KEY, type OnboardingState } from "@/lib/onboarding-types";
import { OrgSwitcher } from "@/components/saas/OrgSwitcher";
import { PlanBanner } from "@/components/saas/PlanBanner";
import { IncomingCallManager } from "@/components/voice/IncomingCallManager";
import StatusSelector from "@/components/ops/StatusSelector";
import NotificationBell from "@/components/notifications/NotificationBell";
import SecurityGate from "@/components/security/SecurityGate";

const MeContext = createContext<Agent | null>(null);
/** Usuario que inició sesión (role, availability...). */
export const useMe = () => useContext(MeContext);

function matches(path: string, href: string) {
  return href === "/" ? path === "/" : path === href || path.startsWith(href + "/");
}

const ALL_HREFS = NAV.flatMap((e) => [e.href, ...(e.groups ?? []).flatMap((g) => g.items.map((i) => i.href))]).filter(
  (h): h is string => !!h,
);

/** Activo solo si es la ruta más específica que coincide (p. ej. /cortex vs /cortex/conexiones). */
function isActive(path: string, href: string) {
  if (!matches(path, href)) return false;
  return !ALL_HREFS.some((h) => h.length > href.length && h.startsWith(href) && matches(path, h));
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
  const [plan, setPlan] = useState<PlanStatus | null>(null);
  const isAdmin = me.role === "admin";

  useEffect(() => {
    api<PlanStatus>("/api/plan").then(setPlan, () => setPlan(null));
  }, []);

  // Empresa sin configurar: el administrador va al asistente (una vez por sesión; «Guardar y salir» lo respeta)
  useEffect(() => {
    if (!isAdmin) return;
    try {
      if (sessionStorage.getItem(ONBOARDING_SKIP_KEY) || sessionStorage.getItem("onboarding_checked")) return;
      sessionStorage.setItem("onboarding_checked", "1");
    } catch {
      return;
    }
    api<OnboardingState>("/api/onboarding").then(
      (s) => {
        if (!s.org.onboarding_completed_at) router.replace("/onboarding");
      },
      () => undefined,
    );
  }, [isAdmin, router]);

  useRealtime((event) => {
    if (event === "alert.new") setAlerts((n) => n + 1);
  });
  useEffect(() => {
    if (path === "/") setAlerts(0);
  }, [path]);

  async function logout() {
    // Cierra la sesión en el servidor (estado «Desconectado», auditoría); si falla igual sale
    await send("/api/auth/logout", "POST").catch(() => undefined);
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
          {NAV.filter((e) => (isAdmin || !e.adminOnly) && (!e.staffOnly || isAdmin || me.role === "supervisor")).map((e) => (
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
            <OrgSwitcher />
            <StatusSelector myAgentId={me.id} />
            <NotificationBell />
            <span className="user">
              {me.name}
              <small className="muted">
                {isAdmin ? "Administrador" : me.role === "supervisor" ? "Supervisor" : "Asesor"}
              </small>
            </span>
            <button onClick={logout}>Salir</button>
          </div>
        </header>
        <SecurityGate />
        <PlanBanner plan={plan} />
        <main className="content">{children}</main>
        <IncomingCallManager />
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
