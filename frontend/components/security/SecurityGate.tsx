"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { loadMe } from "@/lib/security";

/** Montar dentro del panel (Shell): obliga a cambiar la contraseña (temporal / vencida) y a activar el 2FA cuando
 * la política de la empresa lo exige. No pinta nada. */
export default function SecurityGate() {
  const router = useRouter();
  const path = usePathname();

  useEffect(() => {
    let alive = true;
    loadMe()
      .then((me) => {
        if (!alive) return;
        if (me.must_change_password && me.has_password) router.replace("/cambiar-clave");
        else if (me.mfa.required && !me.mfa.enabled && path !== "/perfil/seguridad")
          router.replace("/perfil/seguridad?obligatorio=1");
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [router, path]);

  return null;
}
