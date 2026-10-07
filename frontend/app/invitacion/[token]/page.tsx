"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, fmtDate, setSession, type Agent } from "@/lib/api";
import { ROLE_LABEL, type Invitation } from "@/lib/onboarding-types";

type InvitePreview = { organization: string; email: string; name: string | null; role: Invitation["role"]; expires_at: string };

/** Página pública: el invitado crea su contraseña y entra al panel. */
export default function AcceptInvitationPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  const router = useRouter();
  const [invite, setInvite] = useState<InvitePreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<InvitePreview>(`/api/invitations/accept/${encodeURIComponent(token)}`).then(
      (r) => {
        setInvite(r);
        setName(r.name ?? "");
      },
      (e) => setLoadError(e instanceof Error ? e.message : "La invitación no es válida o ya venció."),
    );
  }, [token]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password.length < 8) return setError("La contraseña debe tener al menos 8 caracteres.");
    if (password !== confirm) return setError("Las contraseñas no coinciden.");
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ access_token: string; agent: Agent }>("/api/invitations/accept", {
        method: "POST",
        body: JSON.stringify({ token, name: name.trim(), password }),
      });
      setSession(r.access_token, r.agent);
      router.replace("/conversaciones");
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo aceptar la invitación");
    } finally {
      setBusy(false);
    }
  }

  if (loadError)
    return (
      <main className="login">
        <div className="card">
          <h1>Invitación no disponible</h1>
          <p className="muted">{loadError}</p>
          <p className="small muted">Pide a quien te invitó que te envíe un enlace nuevo.</p>
        </div>
      </main>
    );

  if (!invite)
    return (
      <main className="login">
        <div className="card">
          <p className="muted">Cargando invitación…</p>
        </div>
      </main>
    );

  return (
    <main className="login">
      <form onSubmit={submit} className="card">
        <h1>Únete a {invite.organization}</h1>
        <p className="muted">
          Te invitaron como <strong>{ROLE_LABEL[invite.role] ?? invite.role}</strong> con el correo {invite.email}. Crea tu contraseña para entrar.
        </p>
        <label>
          Tu nombre
          <input value={name} onChange={(e) => setName(e.target.value)} required autoFocus autoComplete="name" />
        </label>
        <label>
          Contraseña
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} autoComplete="new-password" />
        </label>
        <label>
          Confirma la contraseña
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required minLength={8} autoComplete="new-password" />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={busy}>
          {busy ? "Entrando…" : "Crear cuenta y entrar"}
        </button>
        <p className="small muted">La invitación vence el {fmtDate(invite.expires_at)}.</p>
      </form>
    </main>
  );
}
