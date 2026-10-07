"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { fmtDate, send } from "@/lib/api";
import {
  ORG_STATUS_LABEL,
  ORG_STATUS_TONE,
  fmtLimit,
  type PlanStatus,
  type PublicPlan,
} from "@/lib/saas-types";
import { Badge, Card, ErrorBox, Loading, PageHeader, Stat, useAction, useApi } from "@/components/ui";
import { AdminNotice, ConfigTabs, useIsAdmin } from "@/components/config/common";
import { UsageMeter } from "@/components/saas/UsageMeter";

function CheckoutNotice() {
  const params = useSearchParams();
  const result = params.get("checkout");
  if (result === "ok")
    return <div className="card" style={{ background: "var(--ok-soft)" }}>¡Pago recibido! Tu plan se actualizará en unos segundos.</div>;
  if (result === "cancel") return <div className="card muted">Cancelaste el pago; tu plan no cambió.</div>;
  return null;
}

function PlanCard({
  plan,
  current,
  labels,
  isAdmin,
  busy,
  onChoose,
}: {
  plan: PublicPlan;
  current: boolean;
  labels: PlanStatus["labels"];
  isAdmin: boolean;
  busy: boolean;
  onChoose: () => void;
}) {
  return (
    <Card
      title={
        <span className="inline">
          {plan.name} {current && <Badge tone="ok">Plan actual</Badge>}
        </span>
      }
    >
      <p style={{ fontSize: 24, fontWeight: 700, margin: "0 0 8px" }}>
        US$ {plan.price_month_usd.toLocaleString("es")}
        <span className="muted small"> /mes</span>
      </p>
      {plan.description && <p className="muted small">{plan.description}</p>}
      <ul className="small" style={{ paddingLeft: 18, display: "grid", gap: 2 }}>
        {Object.entries(plan.limits).map(([k, v]) => (
          <li key={k}>
            {labels.metrics[k] ?? k}: <strong>{fmtLimit(v)}</strong>
          </li>
        ))}
        {Object.entries(plan.features).map(([k, on]) => (
          <li key={k} className={on ? "" : "muted"} style={on ? undefined : { textDecoration: "line-through" }}>
            {labels.features[k] ?? k}
          </li>
        ))}
      </ul>
      {isAdmin && !current && (
        <button
          className="primary"
          disabled={busy || !plan.purchasable}
          onClick={onChoose}
          title={plan.purchasable ? undefined : "Este plan se contrata con ventas"}
        >
          {plan.purchasable ? "Elegir este plan" : "Contactar a ventas"}
        </button>
      )}
    </Card>
  );
}

function PlanPage() {
  const isAdmin = useIsAdmin();
  const status = useApi<PlanStatus>("/api/plan");
  const plans = useApi<PublicPlan[]>("/api/plans");
  const [run, busy, error] = useAction();
  const s = status.data;

  async function go(path: string, body?: unknown) {
    const r = await run(() => send<{ url: string }>(path, "POST", body));
    if (r?.url) location.href = r.url;
  }

  return (
    <>
      <PageHeader
        title="Configuraciones"
        subtitle="Plan y facturación: tu suscripción, lo que incluye y cuánto llevas consumido este mes."
        actions={
          isAdmin &&
          s?.subscription?.has_customer && (
            <button onClick={() => go("/api/billing/portal")} disabled={busy}>
              Medio de pago y facturas
            </button>
          )
        }
      />
      <ConfigTabs />
      <AdminNotice />
      <CheckoutNotice />
      <ErrorBox error={status.error || plans.error || error} />
      {!s ? (
        <Loading />
      ) : (
        <>
          <div className="stats">
            <Stat
              label="Estado"
              value={<Badge tone={ORG_STATUS_TONE[s.organization.status]}>{ORG_STATUS_LABEL[s.organization.status]}</Badge>}
              hint={
                s.organization.status === "trial" && s.organization.trial_days_left != null
                  ? `${Math.max(0, s.organization.trial_days_left)} días de prueba`
                  : s.subscription?.current_period_end
                    ? `${s.subscription.cancel_at_period_end ? "Termina" : "Renueva"} el ${fmtDate(s.subscription.current_period_end)}`
                    : undefined
              }
            />
            <Stat label="Plan" value={s.plan?.name ?? "Sin plan (ilimitado)"} />
            <Stat label="Mensajes enviados (mes)" value={s.consumption.messages_out.toLocaleString("es")} />
            <Stat
              label="IA (mes)"
              value={`US$ ${s.consumption.ai_cost_usd.toLocaleString("es", { maximumFractionDigits: 2 })}`}
              hint={`${s.consumption.ai_calls.toLocaleString("es")} llamadas · ${s.consumption.ai_tokens.toLocaleString("es")} tokens`}
            />
          </div>

          <Card title="Uso del plan">
            <div className="grid2">
              {Object.entries(s.usage).map(([k, u]) => (
                <UsageMeter key={k} label={s.labels.metrics[k] ?? k} usage={u} />
              ))}
            </div>
            <p className="small muted" style={{ marginTop: 12 }}>
              Si superas las conversaciones del mes se siguen atendiendo y te avisamos; los demás límites bloquean la
              creación de nuevos elementos.
            </p>
          </Card>

          {plans.data && (
            <div className="grid3" style={{ marginTop: 16 }}>
              {plans.data.map((p) => (
                <PlanCard
                  key={p.key}
                  plan={p}
                  current={s.plan?.key === p.key}
                  labels={s.labels}
                  isAdmin={isAdmin}
                  busy={busy}
                  onChoose={() => go("/api/billing/checkout", { plan_key: p.key })}
                />
              ))}
            </div>
          )}
        </>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <PlanPage />
    </Suspense>
  );
}
