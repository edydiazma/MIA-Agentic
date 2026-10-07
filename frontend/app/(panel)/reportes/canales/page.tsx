"use client";

import { CHANNEL_ICONS, CHANNEL_LABELS, fmtNum, type ChannelProvider } from "@/lib/api";
import { Card, Empty, Stat } from "@/components/ui";
import ReportPage, { useDateRange, useReport } from "@/components/reports/ReportPage";
import { BarList, StackedDaily } from "@/components/reports/charts";

type Row = { conversations: number; inbound: number; outbound: number; contacts_new: number };
type ChannelsReport = {
  totals: Row;
  by_provider: (Row & { provider: ChannelProvider; label: string })[];
  by_channel: (Row & { channel_id: number; name: string; provider: ChannelProvider | null })[];
  series: ({ day: string } & Record<string, number | string>)[];
};

const PROVIDERS = Object.keys(CHANNEL_LABELS) as ChannelProvider[];

export default function CanalesReportPage() {
  const [range, setRange] = useDateRange();
  const { data, error, loading } = useReport<ChannelsReport>("/api/reports/channels", range);
  const t = data?.totals;
  const used = PROVIDERS.filter((p) => data?.by_provider.some((r) => r.provider === p && r.conversations));

  return (
    <ReportPage
      title="Canales"
      subtitle="Conversaciones, mensajes y contactos nuevos por canal: WhatsApp, Messenger, Instagram y chat web."
      range={range}
      onRange={setRange}
      loading={loading}
      error={error}
      ready={!!data}
    >
      {data && t && (
        <>
          <div className="stats">
            <Stat label="Conversaciones nuevas" value={fmtNum(t.conversations)} />
            <Stat label="Mensajes recibidos" value={fmtNum(t.inbound)} />
            <Stat label="Mensajes enviados" value={fmtNum(t.outbound)} />
            <Stat label="Contactos nuevos" value={fmtNum(t.contacts_new)} hint="Por el canal de su primera conversación" />
          </div>
          <Card title="Conversaciones por día y canal">
            {t.conversations === 0 ? (
              <Empty>Sin conversaciones nuevas en este periodo</Empty>
            ) : (
              <StackedDaily
                data={data.series}
                series={(used.length ? used : PROVIDERS).map((p) => ({ key: p, label: CHANNEL_LABELS[p] }))}
                title="Conversaciones nuevas por día y canal"
              />
            )}
          </Card>
          <div className="grid2">
            <Card title="Conversaciones por red">
              <BarList items={data.by_provider.map((r) => ({ label: `${CHANNEL_ICONS[r.provider]} ${r.label}`, value: r.conversations }))} />
            </Card>
            <Card title="Contactos nuevos por red">
              <BarList items={data.by_provider.map((r) => ({ label: `${CHANNEL_ICONS[r.provider]} ${r.label}`, value: r.contacts_new }))} />
            </Card>
          </div>
          <Card title="Detalle por canal">
            {data.by_channel.length === 0 ? (
              <Empty>Sin actividad en este periodo</Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Canal</th>
                      <th className="num">Conversaciones</th>
                      <th className="num">Recibidos</th>
                      <th className="num">Enviados</th>
                      <th className="num">Contactos nuevos</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_channel.map((r) => (
                      <tr key={r.channel_id}>
                        <td>
                          {r.provider ? CHANNEL_ICONS[r.provider] : ""} {r.name}
                          <span className="muted small"> · {r.provider ? CHANNEL_LABELS[r.provider] : "—"}</span>
                        </td>
                        <td className="num">{fmtNum(r.conversations)}</td>
                        <td className="num">{fmtNum(r.inbound)}</td>
                        <td className="num">{fmtNum(r.outbound)}</td>
                        <td className="num">{fmtNum(r.contacts_new)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </ReportPage>
  );
}
