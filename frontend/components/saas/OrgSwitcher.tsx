"use client";

import { useEffect, useState } from "react";
import { api, send, setSession, type Agent } from "@/lib/api";
import type { OrgSummary } from "@/lib/saas-types";

type OrgsResp = { current: number; organizations: OrgSummary[] };
type SwitchResp = { access_token: string; agent: Agent; organization: OrgSummary };

/** Selector de empresa: solo aparece si el usuario validó su contraseña en más de una. */
export function OrgSwitcher() {
  const [data, setData] = useState<OrgsResp | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<OrgsResp>("/api/auth/orgs").then(setData, () => setData(null));
  }, []);

  if (!data || data.organizations.length < 2) return null;

  async function change(id: number) {
    setBusy(true);
    try {
      const r = await send<SwitchResp>("/api/auth/switch-org", "POST", { organization_id: id });
      setSession(r.access_token, r.agent);
      location.href = "/";
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <select
      value={data.current}
      disabled={busy}
      onChange={(e) => change(Number(e.target.value))}
      aria-label="Empresa"
      title="Cambiar de empresa"
    >
      {data.organizations.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name}
        </option>
      ))}
    </select>
  );
}
