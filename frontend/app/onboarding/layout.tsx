"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getAgent, getToken } from "@/lib/api";

/** Asistente a pantalla completa (sin la barra lateral del panel); solo administradores. */
export default function OnboardingLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [ok, setOk] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    if (getAgent()?.role !== "admin") {
      router.replace("/");
      return;
    }
    setOk(true);
  }, [router]);

  return ok ? <>{children}</> : null;
}
