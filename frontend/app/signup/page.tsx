"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, setSession, type Agent } from "@/lib/api";
import type { OrgSummary, SignupConfig } from "@/lib/saas-types";

const COUNTRIES: [string, string][] = [
  ["co", "Colombia"], ["mx", "México"], ["cl", "Chile"], ["pe", "Perú"], ["ar", "Argentina"],
  ["ec", "Ecuador"], ["us", "Estados Unidos"], ["es", "España"], ["", "Otro"],
];

export default function SignupPage() {
  const router = useRouter();
  const [cfg, setCfg] = useState<SignupConfig | null>(null);
  const [form, setForm] = useState({ company: "", name: "", email: "", password: "", country: "co", plan_key: "" });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api<SignupConfig>("/api/signup/config").then(
      (c) => {
        setCfg(c);
        setForm((f) => ({ ...f, plan_key: c.default_plan }));
      },
      (e) => setError(e instanceof Error ? e.message : String(e)),
    );
  }, []);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const r = await api<{ access_token: string; agent: Agent; organization: OrgSummary }>("/api/signup", {
        method: "POST",
        body: JSON.stringify({ ...form, country: form.country || null, plan_key: form.plan_key || null }),
      });
      setSession(r.access_token, r.agent);
      router.replace("/configuraciones/plataforma");
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo crear la cuenta");
    } finally {
      setLoading(false);
    }
  }

  if (cfg && !cfg.enabled)
    return (
      <main className="login">
        <div className="card">
          <h1>Registro cerrado</h1>
          <p className="muted">Por ahora el alta de nuevas empresas es por invitación.</p>
          <Link href="/login">Ir a iniciar sesión</Link>
        </div>
      </main>
    );

  return (
    <main className="login">
      <form onSubmit={submit} className="card" style={{ maxWidth: 440 }}>
        <h1>Crea tu cuenta</h1>
        <p className="muted">
          {cfg ? `${cfg.trial_days} días de prueba gratis, sin tarjeta.` : "Prueba gratis, sin tarjeta."}
        </p>
        <label>
          Empresa
          <input value={form.company} onChange={set("company")} required minLength={2} autoFocus />
        </label>
        <label>
          Tu nombre
          <input value={form.name} onChange={set("name")} required />
        </label>
        <label>
          Correo
          <input type="email" value={form.email} onChange={set("email")} required autoComplete="email" />
        </label>
        <label>
          Contraseña
          <input type="password" value={form.password} onChange={set("password")} required minLength={8} autoComplete="new-password" />
        </label>
        <label>
          País
          <select value={form.country} onChange={set("country")}>
            {COUNTRIES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        {cfg && cfg.plans.length > 0 && (
          <label>
            Plan
            <select value={form.plan_key} onChange={set("plan_key")}>
              {cfg.plans.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.name} · US$ {p.price_month_usd.toLocaleString("es")}/mes
                </option>
              ))}
            </select>
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={loading || !cfg}>
          {loading ? "Creando…" : "Crear cuenta"}
        </button>
        <p className="small muted">
          ¿Ya tienes cuenta? <Link href="/login">Inicia sesión</Link>
        </p>
      </form>
    </main>
  );
}
