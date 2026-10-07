"use client";

import { useState } from "react";
import Link from "next/link";
import { contactLabel, fmtDateTime, qs, type Conversation, phoneLabel } from "@/lib/api";
import { Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";

export default function HistorialPage() {
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const { data, error, loading } = useApi<Conversation[]>(`/api/conversations${qs({ status: "closed", limit: 200, q: search })}`);

  return (
    <>
      <PageHeader title="Historial de conversaciones" subtitle="Conversaciones cerradas, con su tipificación y origen." />
      <Card
        actions={
          <form
            className="inline"
            onSubmit={(e) => {
              e.preventDefault();
              setSearch(q.trim());
            }}
          >
            <input placeholder="Buscar nombre o teléfono" value={q} onChange={(e) => setQ(e.target.value)} />
            <button type="submit">Buscar</button>
          </form>
        }
      >
        <ErrorBox error={error} />
        {loading && !data ? (
          <Loading />
        ) : !data?.length ? (
          <Empty>No hay conversaciones cerradas.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Cliente</th><th>Tipificación</th><th>Asesor</th><th>Grupo</th><th>Origen</th><th>Cerrada</th><th /></tr>
              </thead>
              <tbody>
                {data.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <div className="strong">{contactLabel(c.contact)}</div>
                      <div className="small muted">{phoneLabel(c.contact.wa_id)}</div>
                    </td>
                    <td>{c.typification ?? <span className="muted">—</span>}</td>
                    <td>{c.assigned_agent?.name ?? <span className="muted">Bot</span>}</td>
                    <td>{c.group?.name ?? <span className="muted">—</span>}</td>
                    <td className="small">{c.ad_headline ? `Anuncio: ${c.ad_headline}` : <span className="muted">Orgánico</span>}</td>
                    <td className="small nowrap">{fmtDateTime(c.closed_at)}</td>
                    <td><Link href={`/conversaciones?id=${c.id}`}>Ver</Link></td>
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
