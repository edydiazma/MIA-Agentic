"use client";

import { useState } from "react";
import { fmtDateTime, qs } from "@/lib/api";
import { EVENT_LABEL, usePermissions, type AuthEventOut } from "@/lib/security";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";
import { ConfigTabs } from "@/components/config/common";

const BAD = new Set(["login_failed", "locked", "mfa_failed", "sso_failed", "ip_blocked"]);

export default function AuditPage() {
  const { can, loading } = usePermissions();
  const [event, setEvent] = useState("");
  const [email, setEmail] = useState("");
  const [ip, setIp] = useState("");
  const [days, setDays] = useState(30);
  const path = can("audit.view") ? `/api/security/audit${qs({ event, email, ip, days, limit: 200 })}` : null;
  const { data, error } = useApi<AuthEventOut[]>(path);

  return (
    <>
      <PageHeader title="Auditoría de acceso" subtitle="Ingresos, intentos fallidos, bloqueos, segundo factor, cambios de contraseña, SSO y cambios de roles." />
      <ConfigTabs />
      {loading ? <Loading /> : !can("audit.view") ? <Empty>No tienes permiso para ver la auditoría.</Empty> : (
        <Card
          title="Eventos"
          actions={
            <div className="inline filters">
              <select aria-label="Evento" value={event} onChange={(e) => setEvent(e.target.value)}>
                <option value="">Todos los eventos</option>
                {Object.entries(EVENT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <input aria-label="Correo" placeholder="Correo" value={email} onChange={(e) => setEmail(e.target.value)} />
              <input aria-label="IP" placeholder="IP" value={ip} onChange={(e) => setIp(e.target.value)} />
              <select aria-label="Periodo" value={days} onChange={(e) => setDays(Number(e.target.value))}>
                <option value={1}>Último día</option>
                <option value={7}>7 días</option>
                <option value={30}>30 días</option>
                <option value={90}>90 días</option>
                <option value={365}>12 meses</option>
              </select>
            </div>
          }
        >
          <ErrorBox error={error} />
          {!data ? <Loading /> : data.length === 0 ? <Empty>Sin eventos con esos filtros.</Empty> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Fecha</th><th>Evento</th><th>Usuario</th><th>IP</th><th>Detalle</th></tr></thead>
                <tbody>
                  {data.map((e) => (
                    <tr key={e.id}>
                      <td className="small">{fmtDateTime(e.created_at)}</td>
                      <td><Badge tone={BAD.has(e.event) ? "bad" : e.event === "login_ok" || e.event === "sso_login" ? "ok" : "neutral"}>{EVENT_LABEL[e.event] ?? e.event}</Badge></td>
                      <td>{e.agent_name ?? "—"} <div className="small muted">{e.email}</div></td>
                      <td className="small">{e.ip ?? "—"}</td>
                      <td className="small muted" title={e.user_agent ?? ""}>
                        {Object.entries(e.detail).map(([k, v]) => `${k}: ${String(v)}`).join(" · ") || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
