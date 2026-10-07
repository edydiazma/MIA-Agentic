"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { fmtNum, fmtPct, qs, send } from "@/lib/api";
import { useMe } from "@/components/Shell";
import { Badge, Card, Empty, ErrorBox, Stat, Tabs, useAction, useApi } from "@/components/ui";
import ReportPage, { useDateRange } from "@/components/reports/ReportPage";
import { StackedDaily } from "@/components/reports/charts";
import AdDetailModal from "@/components/ads/AdDetail";
import { PLATFORM_ICON, PLATFORM_LABEL, money, type AdRow, type AdsReport } from "@/lib/golden-types";

type SortKey = keyof Pick<
  AdRow,
  "spend" | "impressions" | "clicks" | "ctr" | "conversations" | "new_contacts" | "sales" | "cost_per_conversation" | "cost_per_new_contact" | "cost_per_sale" | "roas"
>;

const COLUMNS: { key: SortKey; label: string; kind: "money" | "num" | "pct" | "x" }[] = [
  { key: "spend", label: "Inversión", kind: "money" },
  { key: "impressions", label: "Impresiones", kind: "num" },
  { key: "clicks", label: "Clics", kind: "num" },
  { key: "ctr", label: "CTR", kind: "pct" },
  { key: "conversations", label: "Conversaciones", kind: "num" },
  { key: "new_contacts", label: "Clientes nuevos", kind: "num" },
  { key: "sales", label: "Ventas", kind: "num" },
  { key: "cost_per_conversation", label: "Costo / conversación", kind: "money" },
  { key: "cost_per_new_contact", label: "Costo / cliente nuevo", kind: "money" },
  { key: "cost_per_sale", label: "Costo / venta", kind: "money" },
  { key: "roas", label: "ROAS", kind: "x" },
];

export default function AdsReportPage() {
  const [range, setRange] = useDateRange();
  const [platform, setPlatform] = useState("");
  const [campaign, setCampaign] = useState("");
  const [group, setGroup] = useState<"ads" | "campaigns">("ads");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "conversations", desc: true });
  const [open, setOpen] = useState<AdRow | null>(null);
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const [run, syncing, syncError] = useAction();
  const [synced, setSynced] = useState<string | null>(null);

  const report = useApi<AdsReport>(`/api/reports/ads${qs({ ...range, platform, campaign })}`);
  const { data, error, loading } = report;
  const t = data?.totals;
  const cur = t?.currency ?? null;
  const fmt = (kind: (typeof COLUMNS)[number]["kind"], v: number | null | undefined) =>
    kind === "money" ? money(v, cur) : kind === "pct" ? fmtPct(v == null ? null : Math.round(v * 100) / 100) : kind === "x" ? (v == null ? "—" : `${v.toFixed(2)}x`) : fmtNum(v ?? 0);

  const rows = useMemo(() => {
    const list = [...((group === "ads" ? data?.ads : data?.campaigns) ?? [])];
    list.sort((a, b) => {
      const av = a[sort.key] ?? -Infinity;
      const bv = b[sort.key] ?? -Infinity;
      return sort.desc ? (bv as number) - (av as number) : (av as number) - (bv as number);
    });
    return list;
  }, [data, group, sort]);

  const campaigns = useMemo(
    () => Array.from(new Set((data?.campaigns ?? data?.ads ?? []).map((a) => a.campaign_name).filter(Boolean))) as string[],
    [data],
  );

  async function sync() {
    setSynced(null);
    const r = await run(() => send<{ message?: string }>("/api/ads/sync", "POST"));
    if (r !== undefined) {
      setSynced(r?.message ?? "Sincronización iniciada. Los datos se actualizan en unos minutos.");
      report.reload();
    }
  }

  return (
    <ReportPage
      title="Anuncios"
      subtitle={
        <>
          Qué anuncio trajo a cada cliente y cuánto costó: conversaciones, clientes nuevos y ventas atribuidas frente a la
          inversión en Meta y Google Ads. Conecta las cuentas en <Link href="/configuraciones/conversiones">Conversiones</Link>.
        </>
      }
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
      actions={
        isAdmin ? (
          <button onClick={sync} disabled={syncing}>
            {syncing ? "Sincronizando…" : "Sincronizar ahora"}
          </button>
        ) : null
      }
    >
      <ErrorBox error={syncError} />
      {synced && <div className="muted small">{synced}</div>}
      {data && t && (
        <>
          <div className="inline filters" style={{ marginBottom: 8 }}>
            <select value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label="Plataforma">
              <option value="">Todas las plataformas</option>
              <option value="meta">Meta</option>
              <option value="google_ads">Google Ads</option>
              <option value="other">Otras fuentes</option>
            </select>
            <select value={campaign} onChange={(e) => setCampaign(e.target.value)} aria-label="Campaña">
              <option value="">Todas las campañas</option>
              {campaigns.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>

          <div className="stats">
            <Stat label="Inversión" value={money(t.spend, cur)} hint={`${fmtNum(t.impressions)} impresiones · ${fmtNum(t.clicks)} clics`} />
            <Stat label="Conversaciones" value={fmtNum(t.conversations)} hint={`Costo ${money(t.cost_per_conversation, cur)}`} />
            <Stat label="Clientes nuevos" value={fmtNum(t.new_contacts)} hint={`Costo ${money(t.cost_per_new_contact, cur)}`} />
            <Stat label="Ventas" value={fmtNum(t.sales)} hint={`Costo ${money(t.cost_per_sale, cur)}`} />
            <Stat
              label="ROAS"
              value={t.roas == null ? "—" : `${t.roas.toFixed(2)}x`}
              tone={t.roas == null ? undefined : t.roas >= 1 ? "ok" : "bad"}
              hint={`Valor ${money(t.conversion_value + (t.deals_won_value ?? 0), cur)}`}
            />
          </div>

          {data.series.length > 0 && (
            <div className="grid2">
              <Card title="Inversión por día">
                <StackedDaily data={data.series.map((r) => ({ day: r.day, spend: r.spend }))} series={[{ key: "spend", label: "Inversión" }]} title="Inversión por día" />
              </Card>
              <Card title="Conversaciones y ventas por día">
                <StackedDaily
                  data={data.series.map((r) => ({ day: r.day, conversations: r.conversations, sales: r.sales }))}
                  series={[
                    { key: "conversations", label: "Conversaciones" },
                    { key: "sales", label: "Ventas" },
                  ]}
                  title="Conversaciones y ventas por día"
                />
              </Card>
            </div>
          )}

          <Card
            title={group === "ads" ? "Rendimiento por anuncio" : "Rendimiento por campaña"}
            actions={
              <Tabs
                value={group}
                onChange={setGroup}
                tabs={[
                  ["ads", "Anuncios"],
                  ["campaigns", "Campañas"],
                ]}
              />
            }
          >
            {rows.length === 0 ? (
              <Empty>Sin anuncios con datos en este periodo.</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{group === "ads" ? "Anuncio" : "Campaña"}</th>
                      {COLUMNS.map((c) => {
                        const active = sort.key === c.key;
                        return (
                          <th key={c.key} className="num" aria-sort={active ? (sort.desc ? "descending" : "ascending") : undefined}>
                            <button
                              className="link"
                              style={{ color: "inherit", fontWeight: 600, fontSize: "inherit" }}
                              onClick={() => setSort((s) => ({ key: c.key, desc: s.key === c.key ? !s.desc : true }))}
                            >
                              {c.label}
                              {active ? (sort.desc ? " ▼" : " ▲") : ""}
                            </button>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((a) => {
                      const clickable = group === "ads" && !!a.ad_id && a.platform !== "other";
                      return (
                        <tr key={a.ad_key} className={clickable ? "clickable" : undefined} onClick={clickable ? () => setOpen(a) : undefined}>
                          <td>
                            <div className="inline" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
                              {a.thumbnail_url ? (
                                // eslint-disable-next-line @next/next/no-img-element
                                <img className="ad-thumb" src={a.thumbnail_url} alt="" loading="lazy" />
                              ) : (
                                <span className="ad-thumb" aria-hidden style={{ display: "grid", placeItems: "center" }}>
                                  {PLATFORM_ICON[a.platform] ?? "•"}
                                </span>
                              )}
                              <div style={{ minWidth: 0 }}>
                                <div className="strong">
                                  {group === "ads" ? a.ad_name || a.ad_id || a.ad_key : a.campaign_name || a.campaign_id || a.ad_key}
                                </div>
                                {group === "ads" && a.headline && <div className="small">{a.headline}</div>}
                                <div className="muted small">
                                  {PLATFORM_LABEL[a.platform] ?? a.platform}
                                  {group === "ads" && a.campaign_name ? ` · ${a.campaign_name}` : ""}
                                  {a.status && (
                                    <>
                                      {" "}
                                      <Badge tone={a.status === "ACTIVE" ? "ok" : "neutral"}>{a.status}</Badge>
                                    </>
                                  )}
                                </div>
                              </div>
                            </div>
                          </td>
                          {COLUMNS.map((c) => (
                            <td key={c.key} className="num nowrap">
                              {fmt(c.kind, a[c.key])}
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <p className="muted small" style={{ marginTop: 8 }}>
              Conversaciones y ventas por último toque; clientes nuevos por primer toque. Una visita desde una publicación se
              asigna al anuncio que la promocionó cuando la cuenta de Meta está conectada.
            </p>
          </Card>
        </>
      )}
      {open && open.ad_id && (
        <AdDetailModal platform={open.platform} adId={open.ad_id} fallback={open} currency={cur} onClose={() => setOpen(null)} />
      )}
    </ReportPage>
  );
}
