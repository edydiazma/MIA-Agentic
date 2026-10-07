import type { Campaign } from "@/lib/api";
import { Badge } from "@/components/ui";

export const CAMPAIGN_STATUS: Record<Campaign["status"], [string, "neutral" | "info" | "ok" | "bad"]> = {
  draft: ["Borrador", "neutral"],
  running: ["Enviando", "info"],
  done: ["Enviada", "ok"],
  failed: ["Falló", "bad"],
};

export const RECIPIENT_LABEL: Record<string, string> = {
  pending: "Pendiente",
  sent: "Enviado",
  delivered: "Entregado",
  read: "Leído",
  failed: "Fallido",
};

export function CampaignStatusBadge({ status }: { status: Campaign["status"] }) {
  const [label, tone] = CAMPAIGN_STATUS[status] ?? [status, "neutral"];
  return <Badge tone={tone}>{label}</Badge>;
}

export const pct = (a: number, b: number) => (b ? `${Math.round((100 * a) / b)} %` : "—");
