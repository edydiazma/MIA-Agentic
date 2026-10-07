"use client";

import { fmtDateTime } from "@/lib/api";
import { fmtMoney } from "@/lib/crm-types";
import { Badge, Card, Empty, ErrorBox, Loading, useApi } from "@/components/ui";
import { ORDER_STATUS, type ExternalOrder } from "./types";

/** Pedidos de tiendas y conectores del cliente (ficha del cliente). */
export default function ContactOrders({ contactId }: { contactId: number }) {
  const orders = useApi<ExternalOrder[]>(`/api/hub/orders?contact_id=${contactId}&limit=20`);
  return (
    <Card title="Pedidos en tiendas">
      <ErrorBox error={orders.error} />
      {!orders.data ? (
        <Loading />
      ) : orders.data.length === 0 ? (
        <Empty>Sin pedidos vinculados.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Pedido</th>
              <th>Tienda</th>
              <th>Estado</th>
              <th>Total</th>
              <th>Fecha</th>
            </tr>
          </thead>
          <tbody>
            {orders.data.map((o) => (
              <tr key={o.id} title={o.items.map((i) => `${i.quantity ?? 1}× ${i.name ?? i.sku}`).join("\n")}>
                <td>
                  {o.order_number ?? o.external_id}
                  {o.attribution_id && (
                    <span className="small muted" title="Pedido atribuido a una conversación de WhatsApp">
                      {" "}
                      · WhatsApp
                    </span>
                  )}
                </td>
                <td>{o.store}</td>
                <td>
                  <Badge tone={ORDER_STATUS[o.status]?.tone ?? "neutral"}>{ORDER_STATUS[o.status]?.label ?? o.status}</Badge>
                </td>
                <td>{o.total != null ? fmtMoney(o.total, o.currency ?? "COP") : "—"}</td>
                <td className="small">{fmtDateTime(o.placed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
