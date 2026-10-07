"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { getPlatformToken, papi, setPlatformToken } from "@/components/saas/platform-api";

/** Back-office del dueño de la plataforma: sesión y layout propios, independientes del panel de empresas. */
export default function PlatformLayout({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const isLogin = path === "/plataforma/login";
  const [me, setMe] = useState<{ name: string; email: string } | null>(null);

  useEffect(() => {
    if (isLogin) return;
    if (!getPlatformToken()) router.replace("/plataforma/login");
    else papi<{ name: string; email: string }>("/me").then(setMe, () => undefined);
  }, [isLogin, router]);

  if (isLogin) return <>{children}</>;
  if (!me) return null;

  const link = (href: string, label: string) => (
    <Link
      href={href}
      className={(href === "/plataforma" ? path === href : path.startsWith(href)) ? "nav-link active" : "nav-link"}
    >
      {label}
    </Link>
  );

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">◆</span> Plataforma
        </div>
        <nav className="nav">
          {link("/plataforma", "Resumen")}
          {link("/plataforma/empresas", "Empresas")}
          {link("/plataforma/planes", "Planes")}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="topbar-right">
            <span className="user">
              {me.name}
              <small className="muted">{me.email}</small>
            </span>
            <button
              onClick={() => {
                setPlatformToken(null);
                router.replace("/plataforma/login");
              }}
            >
              Salir
            </button>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
