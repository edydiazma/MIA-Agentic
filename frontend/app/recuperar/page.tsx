"use client";

import { useState } from "react";
import Link from "next/link";
import { post } from "@/lib/security";

export default function ForgotPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await post("/api/auth/forgot", { email });
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo enviar");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login">
      {sent ? (
        <div className="card" role="status">
          <h1>Revisa tu correo</h1>
          <p className="muted">
            Si <strong>{email}</strong> tiene una cuenta, te enviamos un enlace para crear una nueva contraseña. Vence en
            30 minutos y solo se puede usar una vez.
          </p>
          <p className="small muted">¿No llega? Revisa spam o pide a un administrador que restablezca tu acceso.</p>
          <Link href="/login">Volver a iniciar sesión</Link>
        </div>
      ) : (
        <form className="card" onSubmit={submit}>
          <h1>Recupera tu contraseña</h1>
          <p className="muted">Escribe el correo con el que ingresas. Te enviaremos un enlace.</p>
          <label>
            Correo
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="primary" disabled={loading}>
            {loading ? "Enviando…" : "Enviar enlace"}
          </button>
          <Link href="/login" className="small">
            Volver
          </Link>
        </form>
      )}
    </main>
  );
}
