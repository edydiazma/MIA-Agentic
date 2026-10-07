"use client";

import { useEffect, useState } from "react";
import { policyHints, post, rawJson, SecurityError, type SecurityPolicy } from "@/lib/security";

/** Cambio de contraseña con las reglas de la política de la empresa. */
export default function ChangePasswordForm({ onDone, submitLabel = "Cambiar contraseña" }: {
  onDone?: () => void;
  submitLabel?: string;
}) {
  const [policy, setPolicy] = useState<Partial<SecurityPolicy> | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    rawJson<Partial<SecurityPolicy>>("/api/auth/password/policy", {}, true).then(setPolicy).catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setOk(false);
    if (next !== confirm) {
      setErrors(["Las contraseñas nuevas no coinciden"]);
      return;
    }
    setBusy(true);
    setErrors([]);
    try {
      await post("/api/auth/password", { current_password: current, new_password: next }, true);
      setOk(true);
      setCurrent("");
      setNext("");
      setConfirm("");
      onDone?.();
    } catch (err) {
      if (err instanceof SecurityError && err.errors.length) setErrors(err.errors);
      else setErrors([err instanceof Error ? err.message : "No se pudo cambiar"]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="stack" style={{ display: "grid", gap: 12, maxWidth: 420 }}>
      <label className="field">
        <span>Contraseña actual</span>
        <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} required
          autoComplete="current-password" />
      </label>
      <label className="field">
        <span>Nueva contraseña</span>
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)} required
          autoComplete="new-password" />
      </label>
      <label className="field">
        <span>Repite la nueva contraseña</span>
        <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required
          autoComplete="new-password" />
      </label>
      {policy && (
        <p className="small muted">
          {policyHints(policy).join(" · ")}
          {policy.history ? ` · no repetir las últimas ${policy.history}` : ""}
        </p>
      )}
      {errors.length > 0 && (
        <ul className="error" role="alert">
          {errors.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
      )}
      {ok && (
        <p className="small" role="status">
          Contraseña actualizada.
        </p>
      )}
      <div>
        <button className="primary" disabled={busy}>
          {busy ? "Guardando…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
