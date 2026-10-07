"use client";

import { useEffect, useState } from "react";
import { send, type Settings } from "@/lib/api";
import { useMe } from "@/components/Shell";
import { ErrorBox, LinkTabs, useAction, useApi } from "@/components/ui";

export const CONFIG_TABS = [
  { href: "/configuraciones/plataforma", label: "Plataforma" },
  { href: "/configuraciones/mensajeria", label: "Mensajería" },
  { href: "/configuraciones/conversaciones", label: "Conversaciones" },
  { href: "/configuraciones/magia", label: "Magia de IA" },
  { href: "/configuraciones/clasificacion", label: "Clasificación IA" },
  { href: "/configuraciones/campos", label: "Campos de cliente" },
  { href: "/configuraciones/datos-maestros", label: "Campos y datos maestros" },
  { href: "/configuraciones/usuarios", label: "Gestión usuarios" },
  { href: "/configuraciones/estados", label: "Estados de asesor" },
  { href: "/configuraciones/horarios", label: "Horarios" },
  { href: "/configuraciones/enrutamiento", label: "Enrutamiento" },
  { href: "/configuraciones/roles", label: "Roles y permisos" },
  { href: "/configuraciones/seguridad", label: "Seguridad" },
  { href: "/configuraciones/auditoria", label: "Auditoría de acceso" },
  { href: "/configuraciones/reportes", label: "Reportes" },
  { href: "/configuraciones/empresa", label: "Mi empresa" },
  { href: "/configuraciones/recursos", label: "Gestor de recursos" },
  { href: "/configuraciones/citas", label: "Citas" },
  { href: "/configuraciones/integraciones", label: "Integraciones" },
  { href: "/configuraciones/atribucion", label: "Atribución web" },
  { href: "/configuraciones/mensajes-disparadores", label: "Mensajes disparadores" },
  { href: "/configuraciones/conversiones", label: "Conversiones" },
  { href: "/configuraciones/api", label: "API y conectores" },
  { href: "/configuraciones/notificaciones", label: "Notificaciones" },
  { href: "/configuraciones/plan", label: "Plan y facturación" },
];

export function ConfigTabs() {
  return <LinkTabs tabs={CONFIG_TABS} />;
}

export function useIsAdmin() {
  return useMe()?.role === "admin";
}

export function AdminNotice() {
  return useIsAdmin() ? null : (
    <div className="error-box" style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>
      Solo los administradores pueden editar esta sección.
    </div>
  );
}

export const DAY_LABELS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];

export function DaysPicker({ value, onChange, disabled }: { value: number[]; onChange: (v: number[]) => void; disabled?: boolean }) {
  return (
    <div className="inline">
      {DAY_LABELS.map((d, i) => (
        <label key={d} className="inline small">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.includes(i)}
            onChange={(e) => onChange(e.target.checked ? [...value, i].sort() : value.filter((x) => x !== i))}
          />
          {d}
        </label>
      ))}
    </div>
  );
}

export const TIMEZONES = [
  "America/Bogota",
  "America/Mexico_City",
  "America/Lima",
  "America/Santiago",
  "America/Argentina/Buenos_Aires",
  "America/Caracas",
  "America/Guayaquil",
  "America/La_Paz",
  "America/Montevideo",
  "America/Asuncion",
  "America/Panama",
  "America/Costa_Rica",
  "America/Guatemala",
  "America/Santo_Domingo",
  "America/Sao_Paulo",
  "America/New_York",
  "Europe/Madrid",
];

/** Carga y guarda una sección de /api/settings/{key} con borrador local. */
export function useSetting<K extends keyof Settings>(key: K) {
  const { data, error, loading } = useApi<Settings[K]>(`/api/settings/${key}`);
  const [draft, setDraft] = useState<Settings[K] | null>(null);
  const [run, busy, saveError] = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  const set = <F extends keyof Settings[K]>(field: F, value: Settings[K][F]) =>
    setDraft((d) => (d ? { ...d, [field]: value } : d));

  const save = async () => {
    if (!draft) return;
    const r = await run(() => send<Settings[K]>(`/api/settings/${key}`, "PUT", draft));
    if (r) {
      setDraft(r);
      setSaved(new Date().toLocaleTimeString("es"));
    }
  };

  return { draft, set, save, busy, error: error || saveError, loading, saved };
}

export function SaveBar({ onSave, busy, saved, error, disabled }: {
  onSave: () => void; busy: boolean; saved: string | null; error: string | null; disabled?: boolean;
}) {
  return (
    <>
      <ErrorBox error={error} />
      <div className="inline">
        <button className="primary" onClick={onSave} disabled={busy || disabled}>
          {busy ? "Guardando…" : "Guardar"}
        </button>
        {saved && <span className="muted small">Guardado a las {saved}</span>}
      </div>
    </>
  );
}

export function copy(text: string) {
  navigator.clipboard?.writeText(text).catch(() => {});
}
