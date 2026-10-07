"use client";

import { fmtDateTime, fmtNum, fmtPct } from "@/lib/api";
import { Badge, Empty, Loading, Modal, Stat, useApi } from "@/components/ui";
import { StackedDaily } from "@/components/reports/charts";
import { PLATFORM_ICON, PLATFORM_LABEL, money, type AdDetail, type AdRow } from "@/lib/golden-types";

/** Ventana con el creativo, el rendimiento y los clientes recientes de un anuncio (GET /api/ads/{platform}/{ad_id}). */
export default function AdDetailModal({
  platform,
  adId,
  fallback,
  currency,
  onClose,
}: {
  platform: string;
  adId: string;
  /** Fila del reporte para mostrar algo mientras carga (o si el detalle no existe aún). */
  fallback?: Partial<AdRow> | null;
  currency?: string | null;
  onClose: () => void;
}) {
  const { data, error, loading } = useApi<AdDetail>(`/api/ads/${encodeURIComponent(platform)}/${encodeURIComponent(adId)}`);
  const d: AdDetail = { ...(fallback ?? {}), ...(data ?? {}) } as AdDetail;
  const creative = d.creative ?? {};
  const headline = creative.headline ?? d.headline ?? fallback?.headline ?? null;
  const body = creative.body ?? d.body ?? null;
  const thumb = creative.thumbnail_url ?? d.thumbnail_url ?? fallback?.thumbnail_url ?? null;
  const media = creative.media_url ?? d.media_url ?? null;
  const mediaType = creative.media_type ?? d.media_type ?? null;
  const t = (d.totals ?? fallback ?? {}) as Partial<AdRow>;
  const contacts = d.contacts ?? d.recent_contacts ?? [];
  const name = d.ad_name ?? d.name ?? fallback?.ad_name ?? adId;

  return (
    <Modal wide title={`${PLATFORM_ICON[platform] ?? ""} ${name}`} onClose={onClose}>
      {loading && !data && !fallback && <Loading />}
      {error && !fallback && <Empty>{/404|no encontrado/i.test(error) ? "Todavía no hay datos de este anuncio." : error}</Empty>}
      <div className="grid2">
        <div className="stack" style={{ gap: 8 }}>
          {mediaType === "video" && media ? (
            <video className="ad-thumb lg" src={media} poster={thumb ?? undefined} controls preload="none" />
          ) : thumb || media ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img className="ad-thumb lg" src={(thumb ?? media) as string} alt={headline ?? "Creativo del anuncio"} />
          ) : (
            <div className="ad-thumb lg muted small" style={{ display: "grid", placeItems: "center", minHeight: 120 }}>
              Sin vista previa del creativo
            </div>
          )}
          {headline && <div className="strong">{headline}</div>}
          {body && (
            <p className="small" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
              {body}
            </p>
          )}
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <dl className="small" style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 10px", margin: 0 }}>
            <dt className="muted">Plataforma</dt>
            <dd style={{ margin: 0 }}>{PLATFORM_LABEL[platform] ?? platform}</dd>
            <dt className="muted">ID del anuncio</dt>
            <dd style={{ margin: 0 }}>
              <code>{adId}</code>
            </dd>
            {(d.campaign_name || fallback?.campaign_name) && (
              <>
                <dt className="muted">Campaña</dt>
                <dd style={{ margin: 0 }}>{d.campaign_name ?? fallback?.campaign_name}</dd>
              </>
            )}
            {d.ad_group_name && (
              <>
                <dt className="muted">{platform === "google_ads" ? "Grupo de anuncios" : "Conjunto de anuncios"}</dt>
                <dd style={{ margin: 0 }}>{d.ad_group_name}</dd>
              </>
            )}
            {(d.status || fallback?.status) && (
              <>
                <dt className="muted">Estado</dt>
                <dd style={{ margin: 0 }}>
                  <Badge tone={(d.status ?? fallback?.status) === "ACTIVE" ? "ok" : "neutral"}>{d.status ?? fallback?.status}</Badge>
                </dd>
              </>
            )}
            {d.destination && (
              <>
                <dt className="muted">Destino</dt>
                <dd style={{ margin: 0 }}>{d.destination}</dd>
              </>
            )}
            {d.post_id && (
              <>
                <dt className="muted">Publicación</dt>
                <dd style={{ margin: 0 }}>
                  <code>{d.post_id}</code>
                </dd>
              </>
            )}
            {d.source_url && (
              <>
                <dt className="muted">Enlace</dt>
                <dd style={{ margin: 0, overflowWrap: "anywhere" }}>
                  <a href={d.source_url} target="_blank" rel="noreferrer noopener">
                    Ver en {PLATFORM_LABEL[platform] ?? "la plataforma"}
                  </a>
                </dd>
              </>
            )}
          </dl>
          <div className="stats">
            <Stat label="Inversión" value={money(t.spend, currency)} />
            <Stat label="Conversaciones" value={fmtNum(t.conversations ?? 0)} hint={`Costo ${money(t.cost_per_conversation, currency)}`} />
            <Stat label="Clientes nuevos" value={fmtNum(t.new_contacts ?? 0)} hint={`Costo ${money(t.cost_per_new_contact, currency)}`} />
            <Stat label="Ventas" value={fmtNum(t.sales ?? 0)} hint={`ROAS ${t.roas != null ? t.roas.toFixed(2) + "x" : "—"}`} />
          </div>
          {t.ctr != null && (
            <span className="muted small">
              {fmtNum(t.impressions ?? 0)} impresiones · {fmtNum(t.clicks ?? 0)} clics · CTR {fmtPct(t.ctr)}
            </span>
          )}
        </div>
      </div>

      {(d.series?.length ?? 0) > 0 && (
        <div style={{ marginTop: 12 }}>
          <StackedDaily
            data={d.series!.map((r) => ({ day: r.day, conversations: r.conversations ?? 0, sales: r.sales ?? 0 }))}
            series={[
              { key: "conversations", label: "Conversaciones" },
              { key: "sales", label: "Ventas" },
            ]}
            title="Conversaciones y ventas por día"
            height={180}
          />
        </div>
      )}

      <h3 style={{ marginTop: 12 }}>Clientes recientes</h3>
      {contacts.length === 0 ? (
        <p className="muted small">Sin clientes atribuidos a este anuncio en el periodo.</p>
      ) : (
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          {contacts.map((c, i) => (
            <li key={c.id ?? c.contact_id ?? i}>
              <span className="strong">{c.name || (c.wa_id ? `+${c.wa_id}` : "Sin nombre")}</span>{" "}
              <span className="muted">{fmtDateTime(c.at ?? c.first_source_at ?? c.created_at ?? null)}</span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
