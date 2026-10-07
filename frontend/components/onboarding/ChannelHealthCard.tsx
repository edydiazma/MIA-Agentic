"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type Channel } from "@/lib/api";
import { Card, Empty } from "@/components/ui";
import type { HealthCheck } from "@/lib/onboarding-types";
import { ChecksList } from "./common";

/** Centro de Control: salud del número de WhatsApp principal (validaciones del asistente que siguen corriendo). */
export default function ChannelHealthCard() {
  const [channel, setChannel] = useState<Channel | null | undefined>(undefined);
  const [checks, setChecks] = useState<HealthCheck[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    api<{ channels: Channel[] }>("/api/channels")
      .then(async (r) => {
        const wa = r.channels.find((c) => !c.provider || c.provider === "whatsapp_cloud") ?? null;
        if (!alive) return;
        setChannel(wa);
        if (!wa) return;
        const h = await api<{ checks: HealthCheck[] }>(`/api/channels/${wa.id}/health`);
        if (alive) setChecks(h.checks);
      })
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, []);

  if (error || channel === undefined) return null;
  const fails = checks?.filter((c) => c.status === "fail").length ?? 0;
  const warns = checks?.filter((c) => c.status === "warn").length ?? 0;
  const summary = !checks ? "" : fails ? `${fails} con falla` : warns ? `${warns} con atención` : "Todo en orden";

  return (
    <Card
      title="Salud del número"
      actions={
        <span className="inline">
          {summary && <span className={`small ${fails ? "ob-text-bad" : warns ? "ob-text-warn" : "ob-text-ok"}`}>{summary}</span>}
          <Link className="small" href="/onboarding">
            Revisar
          </Link>
        </span>
      }
    >
      {!channel ? (
        <Empty>
          Aún no conectas un número de WhatsApp. <Link href="/onboarding">Abrir el asistente</Link>
        </Empty>
      ) : !checks ? (
        <p className="small muted">Consultando…</p>
      ) : checks.length === 0 ? (
        <Empty>Sin validaciones todavía para {channel.display_phone ?? channel.name}.</Empty>
      ) : (
        <ChecksList checks={[...checks].sort((a, b) => rank(a.status) - rank(b.status))} compact />
      )}
    </Card>
  );
}

const rank = (s: HealthCheck["status"]) => ({ fail: 0, warn: 1, pending: 2, pass: 3, skipped: 4 })[s] ?? 5;
