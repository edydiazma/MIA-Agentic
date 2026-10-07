"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { send, timeAgo, fmtNum, type Integration } from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { useMe } from "@/components/Shell";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Stat, useAction, useApi } from "@/components/ui";
import OnboardingBanner from "@/components/onboarding/OnboardingBanner";
import ChannelHealthCard from "@/components/onboarding/ChannelHealthCard";

type CCAlert = {
  id: number;
  severity: "info" | "warning" | "critical";
  layer: string;
  source: "meta" | "system";
  title: string;
  description: string | null;
  ref: string | null;
  created_at: string;
};
type CCCampaign = {
  id: number;
  name: string;
  status: string;
  created_at: string;
  total: number;
  sent: number;
  delivered: number;
  failed: number;
  not_delivered: number;
  read: number;
};
type ControlCenter = {
  updated_at: string;
  cards: {
    meta: { alerts: number };
    campaigns: { total_7d: number; failed: number };
    integrations: { connected: number; total: number };
    webhooks: { total: number; active: number; failing: number; disabled: number };
  };
  meta_summary: {
    accounts_with_alert: number;
    accounts_total: number;
    templates_with_alert: number | null;
    templates_total: number | null;
    opt_out_7d: number;
    opt_out_prev_7d: number;
  };
  alerts: CCAlert[];
  campaigns: CCCampaign[];
  me: { open: number; waiting_unassigned: number; followups_due: number; appointments_today: number };
};

const LAYER: Record<string, string> = {
  contact: "Contacto",
  template: "Plantilla",
  account: "Cuenta",
  phone: "Número",
  campaign: "Campaña",
  webhook: "Webhook",
};
const SEVERITY: Record<CCAlert["severity"], [string, "info" | "warn" | "bad"]> = {
  info: ["Info", "info"],
  warning: ["Alerta", "warn"],
  critical: ["Crítica", "bad"],
};
const CAMPAIGN_STATUS: Record<string, [string, "neutral" | "ok" | "warn" | "bad" | "info"]> = {
  draft: ["Borrador", "neutral"],
  running: ["Enviando", "info"],
  done: ["Enviada", "ok"],
  failed: ["Fallida", "bad"],
};
const PAGE = 6;

function StatusCard({ title, value, tone, href }: { title: string; value: string; tone: "ok" | "warn" | "bad" | "neutral"; href?: string }) {
  const body = (
    <div className={`stat ${tone === "neutral" ? "" : tone}`}>
      <span className="stat-label">{title}</span>
      <span className="strong" style={{ fontSize: 16, color: tone === "neutral" ? undefined : `var(--${tone})` }}>
        {value}
      </span>
    </div>
  );
  return href ? (
    <Link href={href} style={{ color: "inherit", textDecoration: "none" }}>
      {body}
    </Link>
  ) : (
    body
  );
}

export default function HomePage() {
  const me = useMe();
  const cc = useApi<ControlCenter>("/api/control-center");
  const integrations = useApi<Integration[]>("/api/integrations");
  const [page, setPage] = useState(0);
  const [run, busy, actionError] = useAction();

  useRealtime((event) => {
    if (event === "alert.new") cc.reload();
  });
  useEffect(() => {
    const t = setInterval(() => cc.reload(), 60_000);
    return () => clearInterval(t);
  }, [cc.reload]);

  if (!cc.data) return cc.error ? <ErrorBox error={cc.error} /> : <Loading />;
  const d = cc.data;
  const metaAlerts = d.alerts.filter((a) => a.source === "meta");
  const pages = Math.max(1, Math.ceil(d.alerts.length / PAGE));
  const shown = d.alerts.slice(page * PAGE, page * PAGE + PAGE);
  const optDelta = d.meta_summary.opt_out_7d - d.meta_summary.opt_out_prev_7d;
  const wh = d.cards.webhooks;
  const connected = integrations.data?.filter((i) => i.connected).length ?? d.cards.integrations.connected;

  async function resolve(id: number) {
    await run(async () => {
      await send(`/api/alerts/${id}/resolve`, "POST");
      cc.setData({ ...d, alerts: d.alerts.filter((a) => a.id !== id) });
    });
  }

  return (
    <>
      <PageHeader
        title="Centro de Control"
        subtitle={
          <>
            Bienvenido {me?.name}, aquí puedes ver la salud de tu cuenta de WhatsApp, campañas, integraciones y
            webhooks · Actualizado {timeAgo(d.updated_at)}
          </>
        }
        actions={<button onClick={() => cc.reload()} disabled={cc.loading}>Actualizar</button>}
      />
      {me?.role === "admin" && <OnboardingBanner />}

      <div className="stats">
        <StatusCard
          title="Meta"
          value={metaAlerts.length ? `${metaAlerts.length} alertas activas` : "Todo en orden"}
          tone={metaAlerts.some((a) => a.severity === "critical") ? "bad" : metaAlerts.length ? "warn" : "ok"}
        />
        <StatusCard
          title="Campañas"
          value={d.cards.campaigns.failed ? `${d.cards.campaigns.failed} con fallas` : "Todo en orden"}
          tone={d.cards.campaigns.failed ? "warn" : "ok"}
          href="/campanas"
        />
        <StatusCard title="Integraciones" value={`${connected} conectadas`} tone="neutral" />
        <StatusCard
          title="Webhooks"
          value={
            wh.total === 0
              ? "Sin webhooks"
              : wh.failing
                ? `${wh.failing} fallando`
                : wh.disabled
                  ? `${wh.disabled} desactivados`
                  : "Todos activos"
          }
          tone={wh.total === 0 ? "neutral" : wh.failing || wh.disabled ? "warn" : "ok"}
          href="/automatizaciones/webhooks"
        />
      </div>

      <Card title="Mi día">
        <div className="stats" style={{ marginBottom: 0 }}>
          <Link href="/conversaciones" style={{ color: "inherit", textDecoration: "none" }}>
            <Stat label="Mis conversaciones abiertas" value={fmtNum(d.me.open)} />
          </Link>
          <Link href="/conversaciones" style={{ color: "inherit", textDecoration: "none" }}>
            <Stat
              label="En espera sin asignar"
              value={fmtNum(d.me.waiting_unassigned)}
              tone={d.me.waiting_unassigned ? "warn" : undefined}
            />
          </Link>
          <Link href="/seguimiento" style={{ color: "inherit", textDecoration: "none" }}>
            <Stat
              label="Seguimientos vencidos / hoy"
              value={fmtNum(d.me.followups_due)}
              tone={d.me.followups_due ? "warn" : undefined}
            />
          </Link>
          <Link href="/seguimiento" style={{ color: "inherit", textDecoration: "none" }}>
            <Stat label="Citas hoy" value={fmtNum(d.me.appointments_today)} />
          </Link>
        </div>
      </Card>

      <Card title="Alertas de Meta" actions={<span className="muted small">Mensajes de marketing · últimos 7 días</span>}>
        <div className="stats">
          <Stat
            label="Cuentas con alerta"
            value={fmtNum(d.meta_summary.accounts_with_alert)}
            hint={`de ${d.meta_summary.accounts_total} números`}
            tone={d.meta_summary.accounts_with_alert ? "bad" : undefined}
          />
          <Stat
            label="Plantillas con alerta"
            value={d.meta_summary.templates_with_alert == null ? "—" : fmtNum(d.meta_summary.templates_with_alert)}
            hint={d.meta_summary.templates_total == null ? "Configura WA_WABA_ID" : `de ${d.meta_summary.templates_total} en uso`}
            tone={d.meta_summary.templates_with_alert ? "warn" : undefined}
          />
          <Stat
            label="Contactos opt-out"
            value={fmtNum(d.meta_summary.opt_out_7d)}
            hint={
              optDelta === 0
                ? `igual que la semana anterior (${d.meta_summary.opt_out_prev_7d})`
                : `${Math.abs(optDelta)} ${optDelta < 0 ? "menos" : "más"} vs. semana anterior (${d.meta_summary.opt_out_prev_7d})`
            }
          />
        </div>
        <ErrorBox error={actionError} />
        {d.alerts.length === 0 ? (
          <Empty>Sin alertas activas 🎉</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Severidad</th>
                    <th>Alerta</th>
                    <th>Capa</th>
                    <th>Origen</th>
                    <th>Recibida</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {shown.map((a) => (
                    <tr key={a.id}>
                      <td>
                        <Badge tone={SEVERITY[a.severity][1]}>{SEVERITY[a.severity][0]}</Badge>
                      </td>
                      <td>
                        <div className="strong">{a.title}</div>
                        {a.description && <div className="muted small">{a.description}</div>}
                      </td>
                      <td>{LAYER[a.layer] ?? a.layer}</td>
                      <td>{a.source === "meta" ? "Meta" : "Sistema"}</td>
                      <td className="nowrap">{timeAgo(a.created_at)}</td>
                      <td>
                        <button disabled={busy} onClick={() => resolve(a.id)}>
                          Resolver
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <span className="muted small">
                {page * PAGE + 1}–{Math.min(d.alerts.length, (page + 1) * PAGE)} de {d.alerts.length} alertas
              </span>
              <div className="inline">
                <button disabled={page === 0} onClick={() => setPage(page - 1)}>
                  ‹
                </button>
                <button disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
                  ›
                </button>
              </div>
            </div>
          </>
        )}
      </Card>

      <Card
        title="Campañas"
        actions={
          <span className="muted small">
            {d.cards.campaigns.total_7d} campañas en los últimos 7 días · <Link href="/campanas">Ver todas</Link>
          </span>
        }
      >
        {d.campaigns.length === 0 ? (
          <Empty>No hubo campañas en los últimos 7 días.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Campaña</th>
                  <th>Estado</th>
                  <th className="num">Enviados</th>
                  <th className="num">Entregados</th>
                  <th className="num">Fallidos</th>
                  <th className="num">No entregados</th>
                </tr>
              </thead>
              <tbody>
                {d.campaigns.map((c) => {
                  const [label, tone] = CAMPAIGN_STATUS[c.status] ?? [c.status, "neutral" as const];
                  return (
                    <tr key={c.id}>
                      <td>
                        <div className="strong">{c.name}</div>
                        <div className="muted small">{timeAgo(c.created_at)} · WhatsApp</div>
                      </td>
                      <td>
                        <Badge tone={tone}>{label}</Badge>
                      </td>
                      <td className="num">{fmtNum(c.sent)}</td>
                      <td className="num">{fmtNum(c.delivered)}</td>
                      <td className="num">{fmtNum(c.failed)}</td>
                      <td className="num">{fmtNum(c.not_delivered)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <ChannelHealthCard />

      <Card title="Integraciones" actions={<span className="muted small">{connected} conectadas</span>}>
        {integrations.error && <ErrorBox error={integrations.error} />}
        <div className="grid4">
          {(integrations.data ?? []).map((i) => (
            <div key={i.key} className="stat" style={{ gap: 6 }}>
              <div className="row">
                <span className="strong">{i.name}</span>
                <Badge tone="neutral">{i.category}</Badge>
              </div>
              <span className="muted small">{i.description}</span>
              <div className="inline">
                {i.connected ? (
                  <Badge tone="ok">Conectada {i.last_sync_at ? timeAgo(i.last_sync_at) : ""}</Badge>
                ) : (
                  <span className="small muted">{i.status ? "Desconectada" : "Nunca se ha conectado"}</span>
                )}
                {i.last_error && <Badge tone="bad">Error</Badge>}
                {i.href && (
                  <Link href={i.href} className="small">
                    {i.connected ? "Gestionar" : "Conectar"}
                  </Link>
                )}
              </div>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
