"use client";

import { useEffect, useState } from "react";
import { API_URL, getToken, type Agent } from "@/lib/api";

/** Sesión enriquecida (GET /api/auth/me). */
export type MeInfo = Agent & {
  role_info: { id: number | null; key: string; name: string; data_scope: "own" | "groups" | "all" };
  permissions: string[];
  mfa: { enabled: boolean; method: "totp" | "email" | null; required: boolean; recovery_codes_left: number };
  must_change_password: boolean;
  has_password: boolean;
};

export type LoginResult =
  | { choose_org: { id: number; name: string; status: string }[] }
  | { mfa_required: true; mfa_token: string; methods: ("totp" | "email" | "recovery")[]; email_hint: string | null }
  | {
      access_token: string;
      agent: Agent;
      must_change_password?: boolean;
      mfa_enrollment_required?: boolean;
      device_token?: string | null;
    };

export type SecurityPolicy = {
  min_length: number;
  require_upper: boolean;
  require_lower: boolean;
  require_digit: boolean;
  require_symbol: boolean;
  expiry_days: number;
  history: number;
  lockout_attempts: number;
  lockout_minutes: number;
  allowed_ips: string[];
  mfa_required: "none" | "admins" | "all";
  trusted_device_days: number;
  session_timeout_minutes: number;
  breached_check: boolean;
};

export type SSOConnection = {
  id: number;
  protocol: "saml" | "oidc";
  name: string;
  slug: string;
  idp_entity_id: string | null;
  idp_sso_url: string | null;
  idp_certificate: string | null;
  idp_metadata_url: string | null;
  issuer: string | null;
  client_id: string | null;
  has_client_secret: boolean;
  scopes: string[];
  domains: string[];
  jit_provisioning: boolean;
  default_role_id: number | null;
  group_claim: string | null;
  group_mapping: Record<string, { role_key?: string; group_ids?: number[] }>;
  enforce: boolean;
  is_active: boolean;
  last_login_at: string | null;
  sp: { entity_id: string; acs_url: string; metadata_url: string; oidc_redirect_uri: string; login_url: string };
};

export type RoleOut = {
  id: number;
  key: string;
  name: string;
  description: string | null;
  base_role: "admin" | "supervisor" | "agent";
  permissions: string[];
  data_scope: "own" | "groups" | "all";
  is_system: boolean;
  locked: boolean;
  users: number;
};

export type PermissionCatalog = {
  groups: { group: string; permissions: { key: string; label: string }[] }[];
  scopes: string[];
  base_roles: string[];
};

export type AuthEventOut = {
  id: number;
  created_at: string;
  event: string;
  agent_id: number | null;
  agent_name: string | null;
  email: string | null;
  ip: string | null;
  user_agent: string | null;
  detail: Record<string, unknown>;
};

export const EVENT_LABEL: Record<string, string> = {
  login_ok: "Ingreso",
  login_failed: "Ingreso fallido",
  logout: "Salida",
  locked: "Cuenta bloqueada",
  unlocked: "Cuenta desbloqueada",
  mfa_challenge: "Código solicitado",
  mfa_ok: "Segundo factor correcto",
  mfa_failed: "Segundo factor incorrecto",
  mfa_enabled: "2FA activado",
  mfa_disabled: "2FA desactivado",
  password_changed: "Contraseña cambiada",
  password_reset_requested: "Recuperación solicitada",
  password_reset: "Contraseña restablecida",
  sso_login: "Ingreso con SSO",
  sso_failed: "SSO fallido",
  ip_blocked: "IP no permitida",
  role_changed: "Cambio de rol o configuración",
  session_revoked: "Sesión revocada",
};

export const SCOPE_LABEL: Record<string, string> = {
  own: "Solo lo suyo",
  groups: "Sus grupos",
  all: "Toda la empresa",
};

/** Respuesta de error estructurada del backend (detail puede ser texto u objeto). */
export class SecurityError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(
      typeof detail === "string"
        ? detail
        : (detail as { message?: string })?.message ?? "No se pudo completar la operación",
    );
  }
  get errors(): string[] {
    const d = this.detail as { errors?: string[] };
    return Array.isArray(d?.errors) ? d.errors : [];
  }
}

/** fetch JSON sin redirección automática en 401 (para el inicio de sesión y la recuperación). */
export async function rawJson<T>(path: string, init: RequestInit = {}, auth = false): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (auth && token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {}
  if (!res.ok) throw new SecurityError(res.status, (body as { detail?: unknown })?.detail ?? res.statusText);
  return body as T;
}

export const post = <T>(path: string, body: unknown, auth = false) =>
  rawJson<T>(path, { method: "POST", body: JSON.stringify(body) }, auth);

const DEVICE_KEY = "trusted_device";
export function getDeviceToken(): string | null {
  try {
    return localStorage.getItem(DEVICE_KEY);
  } catch {
    return null;
  }
}
export function setDeviceToken(token: string | null) {
  try {
    if (token) localStorage.setItem(DEVICE_KEY, token);
    else localStorage.removeItem(DEVICE_KEY);
  } catch {}
}

let meCache: Promise<MeInfo> | null = null;
/** Sesión con rol y permisos (se pide una vez por carga de página). */
export function loadMe(force = false): Promise<MeInfo> {
  if (!meCache || force) meCache = rawJson<MeInfo>("/api/auth/me", {}, true);
  return meCache;
}

/** Permisos del usuario. `can("users.manage")`; mientras carga devuelve false. Para ocultar menús o botones. */
export function usePermissions() {
  const [me, setMe] = useState<MeInfo | null>(null);
  useEffect(() => {
    let alive = true;
    loadMe()
      .then((m) => alive && setMe(m))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  const set = new Set(me?.permissions ?? []);
  return {
    me,
    loading: me === null,
    can: (perm: string) => set.has(perm),
    canAny: (...perms: string[]) => perms.some((p) => set.has(p)),
  };
}

/** Reglas de la política para mostrarlas junto al campo de contraseña. */
export function policyHints(p: Partial<SecurityPolicy> | null | undefined): string[] {
  if (!p) return [];
  const out = [`Mínimo ${p.min_length ?? 10} caracteres`];
  if (p.require_upper) out.push("una mayúscula");
  if (p.require_lower) out.push("una minúscula");
  if (p.require_digit) out.push("un número");
  if (p.require_symbol) out.push("un símbolo");
  return out;
}
