"use client";

import { useState } from "react";
import { contactLabel, qs, send, timeAgo, type Contact, phoneLabel } from "@/lib/api";
import { Card, Empty, ErrorBox, Loading, PageHeader, useAction, useApi } from "@/components/ui";

export default function ClientesBloqueadosPage() {
  const [q, setQ] = useState("");
  const list = useApi<{ total: number; items: Contact[] }>(`/api/contacts${qs({ blocked: true, q, limit: 500 })}`);
  const [run, busy, error] = useAction();

  async function unblock(c: Contact) {
    if (!window.confirm(`¿Desbloquear a ${contactLabel(c)}? Sus mensajes volverán a llegar a la bandeja.`)) return;
    const ok = await run(() => send(`/api/contacts/${c.id}/unblock`, "POST"));
    if (ok) list.reload();
  }

  return (
    <>
      <PageHeader
        title="Clientes bloqueados"
        subtitle="Los mensajes de estos contactos se ignoran: no llegan a la bandeja ni al bot."
      />
      <Card>
        <input
          style={{ maxWidth: 300, marginBottom: 12 }}
          placeholder="Buscar nombre o teléfono"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <ErrorBox error={list.error || error} />
        {list.loading && !list.data ? (
          <Loading />
        ) : !list.data?.items.length ? (
          <Empty>No hay clientes bloqueados.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Nombre</th>
                  <th>Teléfono</th>
                  <th>Motivo</th>
                  <th>Bloqueado</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((c) => (
                  <tr key={c.id}>
                    <td className="strong">{contactLabel(c)}</td>
                    <td className="nowrap">{phoneLabel(c.wa_id)}</td>
                    <td>{c.blocked_reason ?? <span className="muted">—</span>}</td>
                    <td className="nowrap">{timeAgo(c.blocked_at)}</td>
                    <td className="right">
                      <button disabled={busy} onClick={() => unblock(c)}>
                        Desbloquear
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
