"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getToken, setSession } from "@/lib/api";
import { loadMe } from "@/lib/security";
import ChangePasswordForm from "@/components/security/ChangePasswordForm";

/** Cambio obligatorio: contraseña temporal, vencida por la política o restablecida por un administrador. */
export default function ForcedPasswordChange() {
  const router = useRouter();

  useEffect(() => {
    if (!getToken()) router.replace("/login");
  }, [router]);

  async function done() {
    const me = await loadMe(true).catch(() => null);
    router.replace(me && me.mfa.required && !me.mfa.enabled ? "/perfil/seguridad?obligatorio=1" : "/");
  }

  return (
    <main className="login">
      <div className="card" style={{ maxWidth: 480 }}>
        <h1>Cambia tu contraseña</h1>
        <p className="muted">
          Por seguridad debes definir una contraseña nueva antes de continuar (es temporal, venció o la restableció un
          administrador).
        </p>
        <ChangePasswordForm onDone={done} submitLabel="Guardar y continuar" />
        <button
          type="button"
          className="link small"
          onClick={() => {
            setSession(null);
            router.replace("/login");
          }}
        >
          Salir
        </button>
      </div>
    </main>
  );
}
