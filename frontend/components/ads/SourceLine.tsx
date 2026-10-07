"use client";

import { useState } from "react";
import { fmtDateTime } from "@/lib/api";
import { channelLabel } from "@/lib/attribution-types";
import { SOURCE_CHANNEL_ICON, platformOf } from "@/lib/golden-types";
import AdDetailModal from "./AdDetail";

/** Campos de fuente del cliente (contacts.first_source_* / last_source_*, docs/data-model.md §16). */
export type SourceFields = {
  first_source_channel?: string | null;
  first_source_ad_id?: string | null;
  first_source_campaign?: string | null;
  first_source_label?: string | null;
  first_source_at?: string | null;
  last_source_channel?: string | null;
  last_source_ad_id?: string | null;
  last_source_campaign?: string | null;
  last_source_label?: string | null;
  last_source_at?: string | null;
};

type Which = "first" | "last";

function pick(s: SourceFields, which: Which) {
  return which === "first"
    ? { channel: s.first_source_channel, adId: s.first_source_ad_id, label: s.first_source_label, campaign: s.first_source_campaign, at: s.first_source_at }
    : { channel: s.last_source_channel, adId: s.last_source_ad_id, label: s.last_source_label, campaign: s.last_source_campaign, at: s.last_source_at };
}

const text = (p: ReturnType<typeof pick>) => p.label || [channelLabel(p.channel), p.campaign].filter(Boolean).join(" · ");

/** Celda de la lista de clientes: icono de plataforma + etiqueta; clic abre el anuncio. */
export function SourceCell({ row, which = "first" }: { row: SourceFields; which?: Which }) {
  const [open, setOpen] = useState(false);
  const p = pick(row, which);
  if (!p.channel && !p.label) return <span className="muted">—</span>;
  const icon = SOURCE_CHANNEL_ICON[p.channel ?? ""] ?? "•";
  const platform = platformOf(p.channel);
  const title = `${text(p)}${p.at ? `\n${fmtDateTime(p.at)}` : ""}${p.adId ? `\nAnuncio ${p.adId}` : ""}`;
  const content = (
    <>
      <span aria-hidden>{icon}</span> {text(p)}
    </>
  );
  return (
    <>
      {p.adId && platform !== "other" ? (
        <button
          className="link"
          style={{ fontSize: "inherit", textAlign: "left" }}
          title={title}
          onClick={(e) => {
            e.stopPropagation();
            setOpen(true);
          }}
        >
          {content}
        </button>
      ) : (
        <span title={title}>{content}</span>
      )}
      {open && p.adId && (
        <span onClick={(e) => e.stopPropagation()}>
          <AdDetailModal platform={platform} adId={p.adId} onClose={() => setOpen(false)} />
        </span>
      )}
    </>
  );
}

/** Línea compacta "Fuente: Meta · Campaña · Anuncio" para la ficha resumen; el tooltip muestra la última fuente. */
export function SourceLine({ contact }: { contact: SourceFields }) {
  const first = pick(contact, "first");
  if (!first.channel && !first.label) return null;
  const last = pick(contact, "last");
  const lastText = last.channel || last.label ? `Última fuente: ${text(last)}${last.at ? ` (${fmtDateTime(last.at)})` : ""}` : "";
  return (
    <div className="source-line" title={lastText || undefined}>
      <span className="muted">Fuente:</span>
      <SourceCell row={contact} which="first" />
    </div>
  );
}
