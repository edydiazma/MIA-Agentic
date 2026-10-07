"use client";

import { useEffect, useState } from "react";
import { send } from "@/lib/api";
import { useApi } from "@/components/ui";
import { useRealtime } from "@/lib/realtime";
import { fmtDuration, type AgentStatusDef, type MyStatus } from "@/lib/ops-types";

const HEARTBEAT_MS = 60_000;

/**
 * Selector del estado del asesor (Disponible, Almuerzo, Capacitación…) con el tiempo en el estado.
 * También envía el latido de la sesión cada minuto (sin latido la sesión expira y pasa a desconectado).
 * Para montarlo en la barra superior: <StatusSelector />.
 */
export default function StatusSelector({ myAgentId }: { myAgentId?: number }) {
  const statuses = useApi<AgentStatusDef[]>("/api/agent-statuses");
  const mine = useApi<MyStatus>("/api/me/status");
  const [current, setCurrent] = useState<MyStatus | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { if (mine.data) setCurrent(mine.data); }, [mine.data]);

  useEffect(() => {
    const beat = () => { send("/api/me/heartbeat", "POST").catch(() => undefined); };
    beat();
    const hb = setInterval(beat, HEARTBEAT_MS);
    const tick = setInterval(() => setNow(Date.now()), 30_000);
    return () => { clearInterval(hb); clearInterval(tick); };
  }, []);

  useRealtime((event, data: any) => {
    if (event === "agent.status" && myAgentId && data?.agent_id === myAgentId) {
      setCurrent({ status: data.status, since: data.since, availability: data.availability });
    }
  });

  const options = (statuses.data ?? []).filter((s) => s.is_active && !s.is_offline);
  const st = current?.status ?? null;
  const since = current?.since ? (now - new Date(current.since).getTime()) / 1000 : null;

  async function change(id: number) {
    setError(null);
    try {
      const r = await send<MyStatus>("/api/me/status", "PUT", { status_id: id });
      setCurrent(r);
    } catch (e: any) {
      setError(e?.message ?? "No se pudo cambiar el estado");
    }
  }

  return (
    <span className="inline small" title={error ?? (st ? `${st.name} desde hace ${fmtDuration(since)}` : "")}>
      <span aria-hidden style={{ color: st?.color ?? undefined }}>{st?.icon ?? "●"}</span>
      <label className="sr-only" htmlFor="agent-status-select">Mi estado</label>
      <select
        id="agent-status-select"
        value={st?.id ?? ""}
        onChange={(e) => change(Number(e.target.value))}
        style={{ width: "auto" }}
      >
        {!st && <option value="">Estado…</option>}
        {st && !options.some((o) => o.id === st.id) && <option value={st.id}>{st.name}</option>}
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}{o.receives_conversations ? "" : " · no recibe"}
          </option>
        ))}
      </select>
      {since != null && <span className="muted">{fmtDuration(since)}</span>}
      {error && <span className="error small" role="alert">{error}</span>}
    </span>
  );
}
