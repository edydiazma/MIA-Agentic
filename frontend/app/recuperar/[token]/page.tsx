"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { policyHints, post, rawJson, SecurityError, type SecurityPolicy } from "@/lib/security";

export default function ResetPage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const [info, setInfo] = useState<{ email: string; policy: Partial<SecurityPolicy> } | null>(null);
  const [invalid, setInvalid] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    rawJson<{ email: string; policy: Partial<SecurityPolicy> }>(`/api/auth/reset/${encodeURIComponent(token)}`)
      .then(setInfo)
      .catch((e) => setInvalid(e instanceof Error ? e.message : "Enlace inválido"));
  }, [token]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setErrors(["Las contraseñas no coinciden"]);
      return;
    }
    setLoading(true);
    setErrors([]);
    try {
      await post("/api/auth/reset", { token, password });
      setDone(true);
      setTimeout(() => router.replace("/login"), 2500);
    } catch (err) {
      if (err instanceof SecurityError && err.errors.length) setErrors(err.errors);
      else if (err instanceof SecurityError && err.status === 410) setInvalid(err.message);
      else setErrors([err instanceof Error ? err.message : "No se pudo guardar"]);
    } finally {
      setLoading(false);
    }
  }

  if (invalid)
    return (
      <main className="login">
        <div className="card" role="alert">
          <h1>Enlace no válido</h1>
          <p className="muted">{invalid}</p>
          <Link href="/recuperar">Pedir un enlace nuevo</Link>
        </div>
      </main>
    );
  if (done)
    return (
      <main className="login">
        <div className="card" role="status">
          <h1>Contraseña actualizada</h1>
          <p className="muted">Ya puedes ingresar con tu nueva contraseña.</p>
          <Link href="/login">Ir a iniciar sesión</Link>
        </div>
      </main>
    );
  return (
    <main className="login">
      <form className="card" onSubmit={submit}>
        <h1>Nueva contraseña</h1>
        <p className="muted">{info ? `Para ${info.email}` : "Verificando el enlace…"}</p>
        <label>
          Nueva contraseña
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
            autoComplete="new-password" autoFocus />
        </label>
        <label>
          Repite la contraseña
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required
            autoComplete="new-password" />
        </label>
        {info && <p className="small muted">{policyHints(info.policy).join(" · ")}</p>}
        {errors.length > 0 && (
          <ul className="error" role="alert">
            {errors.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        )}
        <button className="primary" disabled={loading || !info}>
          {loading ? "Guardando…" : "Guardar contraseña"}
        </button>
      </form>
    </main>
  );
}
