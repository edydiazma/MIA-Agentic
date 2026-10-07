"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { fmtDate } from "@/lib/api";
import {
  ORG_STATUS_LABEL,
  ORG_STATUS_TONE,
  type OrgStatus,
  type PlatformOrgDetail,
  type PlatformPlan,
} from "@/lib/saas-types";
import { Badge, Card, ErrorBox, Field, Loading, PageHeader, Stat } from "@/components/ui";
import { UsageMeter } from "@/components/saas/UsageMeter";
import { papi, usePlatform } from "@/components/saas/platform-api";

export default function OrgDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = usePlatform<PlatformOrgDetail>(`/orgs/${id}`);
  const plans = usePlatform<PlatformPlan[]>("/plans").data ?? [];
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function update(body: Record<string, unknown>, confirmText?: string) {
    if (confirmText && !confirm(confirmText)) return;
    setBusy(true);
    setActionError(null);
    try {
      await papi(`/orgs/${id}`, "PUT", body);
      await reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  const org = data.organization;

  return (
    <>
      <PageHeader
        title={org.name}
        subtitle={<Link href="/plataforma/empresas">← Empresas</Link>}
        actions={<Badge tone={ORG_STATUS_TONE[org.status]}>{ORG_STATUS_LABEL[org.status]}</Badge>}
      />
      <ErrorBox error={actionError} />
      <div className="stats">
        <Stat label="Plan" value={data.plan?.name ?? "Sin plan"} />
        <Stat
          label="Suscripción"
          value={data.subscription ? `${data.subscription.provider} · ${data.subscription.status}` : "—"}
          hint={data.subscription?.current_period_end ? `Hasta ${fmtDate(data.subscription.current_period_end)}` : undefined}
        />
        <Stat
          label="Prueba"
          value={org.trial_days_left != null ? `${Math.max(0, org.trial_days_left)} días` : "—"}
          hint={org.trial_ends_at ? `Vence ${fmtDate(org.trial_ends_at)}` : undefined}
        />
        <Stat label="Conversaciones (total)" value={data.conversations_total.toLocaleString("es")} />
        <Stat
          label="IA (mes)"
          value={`US$ ${data.consumption.ai_cost_usd.toLocaleString("es", { maximumFractionDigits: 2 })}`}
          hint={`${data.consumption.messages_out.toLocaleString("es")} mensajes enviados`}
        />
      </div>

      <div className="grid2">
        <Card title="Uso del plan">
          <div style={{ display: "grid", gap: 10 }}>
            {Object.entries(data.usage).map(([k, u]) => (
              <UsageMeter key={k} label={data.labels.metrics[k] ?? k} usage={u} />
            ))}
          </div>
        </Card>

        <Card title="Gestión">
          <div style={{ display: "grid", gap: 12 }}>
            <Field label="Plan" hint="Si paga con Stripe, el próximo evento del proveedor puede volver a cambiarlo.">
              <select
                value={data.plan?.id ?? ""}
                disabled={busy}
                onChange={(e) => update({ plan_id: Number(e.target.value) }, "¿Cambiar el plan de esta empresa?")}
              >
                {!data.plan && <option value="">Sin plan</option>}
                {plans.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · US$ {p.price_month_usd}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Estado">
              <select
                value={org.status}
                disabled={busy}
                onChange={(e) =>
                  update({ status: e.target.value }, `¿Cambiar el estado a «${ORG_STATUS_LABEL[e.target.value as OrgStatus]}»?`)
                }
              >
                {(Object.keys(ORG_STATUS_LABEL) as OrgStatus[]).map((s) => (
                  <option key={s} value={s}>
                    {ORG_STATUS_LABEL[s]}
                  </option>
                ))}
              </select>
            </Field>
            <div className="inline">
              <input type="number" min={1} max={90} value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: 80 }} />
              <button disabled={busy || days < 1} onClick={() => update({ extend_trial_days: days })}>
                Extender prueba
              </button>
            </div>
            <div className="inline">
              {org.status !== "suspended" ? (
                <button
                  disabled={busy}
                  style={{ color: "var(--bad)" }}
                  onClick={() => update({ status: "suspended" }, "La empresa perderá el acceso de inmediato. ¿Suspender?")}
                >
                  Suspender
                </button>
              ) : (
                <button disabled={busy} onClick={() => update({ status: "active" }, "¿Reactivar la empresa?")}>
                  Reactivar
                </button>
              )}
            </div>
          </div>
        </Card>
      </div>

      <Card title="Administradores">
        <table className="table">
          <tbody>
            {data.admins.map((a) => (
              <tr key={a.email}>
                <td>{a.name}</td>
                <td className="muted">{a.email}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
