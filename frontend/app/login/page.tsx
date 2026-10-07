"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, setSession } from "@/lib/api";
import type { LoginResponse, OrgSummary } from "@/lib/saas-types";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [orgs, setOrgs] = useState<OrgSummary[] | null>(null);

  async function submit(e: React.FormEvent | null, organizationId?: number) {
    e?.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const r = await api<LoginResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password, organization_id: organizationId ?? null }),
      });
      if ("choose_org" in r) {
        setOrgs(r.choose_org);
        return;
      }
      setSession(r.access_token, r.agent);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error al iniciar sesión");
    } finally {
      setLoading(false);
    }
  }

  if (orgs)
    return (
      <main className="login">
        <div className="card">
          <h1>Elige la empresa</h1>
          <p className="muted">Tu correo tiene acceso a varias empresas. Podrás cambiar luego desde la barra superior.</p>
          {orgs.map((o) => (
            <button key={o.id} disabled={loading} onClick={() => submit(null, o.id)} style={{ textAlign: "left" }}>
              {o.name}
            </button>
          ))}
          {error && <p className="error">{error}</p>}
          <button className="link" onClick={() => setOrgs(null)}>
            Volver
          </button>
        </div>
      </main>
    );

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <h1>Bandeja WhatsApp</h1>
        <p className="muted">Ingresa con tu cuenta de asesor</p>
        <label>
          Correo
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label>
          Contraseña
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={loading}>
          {loading ? "Ingresando…" : "Ingresar"}
        </button>
        <p className="small muted">
          ¿Tu empresa aún no tiene cuenta? <Link href="/signup">Regístrate</Link>
        </p>
      </form>
    </main>
  );
}
