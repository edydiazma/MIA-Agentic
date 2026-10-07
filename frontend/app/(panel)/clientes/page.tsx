"use client";

import { useState } from "react";
import { STAGE_LABEL, contactLabel, fmtDate, qs, type Contact, type Stage, phoneLabel } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";
import ContactDetail from "@/components/clients/ContactDetail";
import { ImportModal, NewContactModal } from "@/components/clients/ContactModals";

const PAGE = 50;
const STAGE_TONE: Record<Stage, "neutral" | "info" | "ok" | "bad"> = {
  lead: "neutral",
  prospect: "info",
  client: "ok",
  lost: "bad",
};

export default function ClientesPage() {
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [stage, setStage] = useState("");
  const [offset, setOffset] = useState(0);
  const [modal, setModal] = useState<"new" | "import" | null>(null);
  const [detail, setDetail] = useState<number | null>(null);

  const list = useApi<{ total: number; items: Contact[] }>(
    `/api/contacts${qs({ q, tag, stage, offset, limit: PAGE })}`,
  );
  const tags = useApi<{ tag: string; count: number }[]>("/api/contacts/tags");
  const total = list.data?.total ?? 0;

  const reset = (fn: () => void) => {
    fn();
    setOffset(0);
  };

  return (
    <>
      <PageHeader
        title="Clientes"
        subtitle={`${total.toLocaleString("es")} clientes`}
        actions={
          <>
            <button onClick={() => setModal("import")}>Importar CSV</button>
            <button className="primary" onClick={() => setModal("new")}>
              Nuevo cliente
            </button>
          </>
        }
      />
      <Card>
        <div className="inline" style={{ marginBottom: 12 }}>
          <input
            style={{ maxWidth: 300 }}
            placeholder="Buscar nombre, teléfono o email"
            value={q}
            onChange={(e) => reset(() => setQ(e.target.value))}
          />
          <select style={{ maxWidth: 200 }} value={tag} onChange={(e) => reset(() => setTag(e.target.value))}>
            <option value="">Todas las etiquetas</option>
            {tags.data?.map((t) => (
              <option key={t.tag} value={t.tag}>
                {t.tag} ({t.count})
              </option>
            ))}
          </select>
          <select style={{ maxWidth: 180 }} value={stage} onChange={(e) => reset(() => setStage(e.target.value))}>
            <option value="">Todas las etapas</option>
            {(Object.keys(STAGE_LABEL) as Stage[]).map((s) => (
              <option key={s} value={s}>
                {STAGE_LABEL[s]}
              </option>
            ))}
          </select>
        </div>
        <ErrorBox error={list.error} />
        {list.loading && !list.data ? (
          <Loading />
        ) : !list.data?.items.length ? (
          <Empty>No hay clientes con esos filtros.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Nombre</th>
                  <th>Teléfono</th>
                  <th>Email</th>
                  <th>Etapa</th>
                  <th>Etiquetas</th>
                  <th>Creado</th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((c) => (
                  <tr key={c.id} className="clickable" onClick={() => setDetail(c.id)}>
                    <td>
                      <span className="strong">{contactLabel(c)}</span>{" "}
                      {c.marketing_opt_out && <Badge tone="warn">Opt-out</Badge>}
                    </td>
                    <td className="nowrap">{phoneLabel(c.wa_id)}</td>
                    <td>{c.email ?? "—"}</td>
                    <td>
                      <Badge tone={STAGE_TONE[c.stage]}>{STAGE_LABEL[c.stage]}</Badge>
                    </td>
                    <td>
                      {c.tags.map((t) => (
                        <span key={t} className="tag">
                          {t}
                        </span>
                      ))}
                    </td>
                    <td className="nowrap">{fmtDate(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {total > PAGE && (
          <div className="row" style={{ marginTop: 12 }}>
            <span className="muted small">
              {offset + 1}–{Math.min(offset + PAGE, total)} de {total.toLocaleString("es")}
            </span>
            <div className="inline">
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Anterior
              </button>
              <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
                Siguiente
              </button>
            </div>
          </div>
        )}
      </Card>

      {modal === "new" && (
        <NewContactModal
          onClose={() => setModal(null)}
          onCreated={(c) => {
            setModal(null);
            list.reload();
            tags.reload();
            setDetail(c.id);
          }}
        />
      )}
      {modal === "import" && (
        <ImportModal
          onClose={() => setModal(null)}
          onDone={() => {
            list.reload();
            tags.reload();
          }}
        />
      )}
      {detail !== null && (
        <ContactDetail
          contactId={detail}
          onClose={() => setDetail(null)}
          onChanged={() => {
            list.reload();
            tags.reload();
          }}
        />
      )}
    </>
  );
}
