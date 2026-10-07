"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { setSession, type Agent } from "@/lib/api";
import { post } from "@/lib/security";

function Callback() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const done = useRef(false);

  useEffect(() => {
    if (done.current) return; // el código es de un solo uso (StrictMode monta dos veces)
    done.current = true;
    const code = params.get("code");
    if (!code) {
      setError("Falta el código de inicio de sesión");
      return;
    }
    post<{ access_token: string; agent: Agent; must_change_password?: boolean }>("/api/auth/sso/exchange", { code })
      .then((r) => {
        setSession(r.access_token, r.agent);
        window.history.replaceState(null, "", "/sso/callback");
        router.replace("/");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "No se pudo iniciar sesión"));
  }, [params, router]);

  return (
    <main className="login">
      <div className="card" role={error ? "alert" : "status"}>
        <h1>{error ? "No se pudo iniciar sesión" : "Ingresando…"}</h1>
        {error && (
          <>
            <p className="muted">{error}</p>
            <Link href="/login">Volver a iniciar sesión</Link>
          </>
        )}
      </div>
    </main>
  );
}

export default function SsoCallbackPage() {
  return (
    <Suspense fallback={null}>
      <Callback />
    </Suspense>
  );
}
