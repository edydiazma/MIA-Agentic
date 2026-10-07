"use client";

import { useState } from "react";
import { fmtDateTime } from "@/lib/api";
import { Badge, useApi } from "@/components/ui";
import { MATCHED_BY_LABEL, type ConversationAttribution } from "@/lib/attribution-types";

/** Sección «Origen» del panel del contacto: último toque, enlace, campaña/anuncio y recorrido de toques. */
export default function ConversationOrigin({ conversationId, adHeadline }: { conversationId: number; adHeadline: string | null }) {
  const { data, error } = useApi<ConversationAttribution>(`/api/attributions/conversation/${conversationId}`);
  const [showTouches, setShowTouches] = useState(false);

  // Sin el módulo de atribución (plan) o sin datos: lo que ya sabemos del referral de Meta
  if (error || !data) {
    return adHeadline ? (
      <div>
        <h3>Origen</h3>
        <p className="small" style={{ margin: 0 }}>
          📣 Llegó desde el anuncio «{adHeadline}»
        </p>
      </div>
    ) : null;
  }
  if (data.channel === "direct" && (data.touches?.length ?? 0) <= 1) {
    return (
      <div>
        <h3>Origen</h3>
        <p className="small muted" style={{ margin: 0 }}>
          Directo (sin anuncio, enlace ni código)
        </p>
      </div>
    );
  }

  const campaign = data.campaign_name || data.utm_campaign;
  const touches = data.touches ?? [];
  return (
    <div>
      <h3>
        Origen <Badge tone="info">{data.label}</Badge>
      </h3>
      <div className="small stack" style={{ gap: 2 }}>
        {data.link && (
          <div>
            🔗 Mensaje disparador: <strong>{data.link.name}</strong>
          </div>
        )}
        {adHeadline && data.channel === "meta_ctwa" && <div>📣 Anuncio «{adHeadline}»</div>}
        {campaign && <div>Campaña: {campaign}</div>}
        {data.ad_group_name && <div>Conjunto / grupo: {data.ad_group_name}</div>}
        {data.ad_name && <div>Anuncio: {data.ad_name}</div>}
        {(data.keyword || data.utm_term) && <div>Palabra clave: {data.keyword || data.utm_term}</div>}
        {(data.utm_source || data.utm_medium) && (
          <div className="muted">
            {[data.utm_source, data.utm_medium].filter(Boolean).join(" / ")}
            {data.utm_content ? ` · ${data.utm_content}` : ""}
          </div>
        )}
        <div className="muted">
          Identificado por: {MATCHED_BY_LABEL[data.matched_by] ?? data.matched_by}
          {data.gclid && " · gclid"}
          {data.ctwa_clid && " · ctwa_clid"}
        </div>
        {data.enrichment_status === "pending" && (
          <div className="muted">Consultando nombres de campaña y anuncio en la plataforma…</div>
        )}
        {data.enrichment_status === "failed" && <div className="muted">No se pudieron leer los nombres de campaña.</div>}
      </div>
      {touches.length > 1 && (
        <div style={{ marginTop: 6 }}>
          <button className="link small" onClick={() => setShowTouches(!showTouches)}>
            {showTouches ? "Ocultar recorrido" : `Ver recorrido (${touches.length} toques)`}
          </button>
          {showTouches && (
            <ol className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {touches.map((t) => (
                <li key={t.id} style={{ marginBottom: 4 }}>
                  <span className="muted">{fmtDateTime(t.occurred_at)}</span> · {t.label}
                  {t.link && <> · {t.link.name}</>}
                  {!t.link && t.utm_campaign && <> · {t.utm_campaign}</>}
                  {t.is_first && (
                    <>
                      {" "}
                      <Badge tone="neutral">Primer toque</Badge>
                    </>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
