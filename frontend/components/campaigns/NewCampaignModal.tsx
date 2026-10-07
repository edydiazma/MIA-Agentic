"use client";

import { useMemo, useState } from "react";
import { send, type Campaign, type Template } from "@/lib/api";
import { Badge, ErrorBox, Field, Loading, Modal, useAction, useApi } from "@/components/ui";

export default function NewCampaignModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const templates = useApi<Template[]>("/api/templates");
  const tags = useApi<{ tag: string; count: number }[]>("/api/contacts/tags");
  const segments = useApi<{ id: number; name: string; member_count: number }[]>("/api/segments");
  const [name, setName] = useState("");
  const [tplKey, setTplKey] = useState("");
  const [values, setValues] = useState<string[]>([]);
  const [audience, setAudience] = useState<"tag" | "segment" | "phones">("tag");
  const [tag, setTag] = useState("");
  const [segmentId, setSegmentId] = useState("");
  const [phones, setPhones] = useState("");
  const [created, setCreated] = useState<Campaign | null>(null);
  const [run, busy, error] = useAction();

  const usable = useMemo(
    () => (templates.data ?? []).filter((t) => t.status === "APPROVED" && t.supported),
    [templates.data],
  );
  const tpl = usable.find((t) => `${t.name}|${t.language}` === tplKey) ?? null;

  function pickTemplate(key: string) {
    setTplKey(key);
    const t = usable.find((x) => `${x.name}|${x.language}` === key);
    setValues(t ? t.variables.map(() => "") : []);
  }

  const phoneList = phones.split(/\n|,|;/).map((p) => p.trim()).filter(Boolean);
  const ready =
    name.trim() && tpl && values.every((v) => v.trim()) && (audience === "tag" ? !!tag : audience === "segment" ? !!segmentId : phoneList.length > 0);

  async function create() {
    if (!tpl) return;
    const c = await run(() =>
      send<Campaign>("/api/campaigns", "POST", {
        name: name.trim(),
        template_name: tpl.name,
        template_language: tpl.language,
        params: values,
        ...(audience === "tag"
          ? { tag }
          : audience === "segment"
            ? { segment_id: Number(segmentId) }
            : { phones: phoneList }),
      }),
    );
    if (c) {
      setCreated(c);
      onDone();
    }
  }

  async function start() {
    if (!created) return;
    const ok = await run(() => send(`/api/campaigns/${created.id}/start`, "POST"));
    if (ok) {
      onDone();
      onClose();
    }
  }

  if (created)
    return (
      <Modal
        title="Campaña creada"
        onClose={onClose}
        footer={
          <>
            <button onClick={onClose}>Guardar como borrador</button>
            <button className="primary" disabled={busy} onClick={start}>
              {busy ? "Iniciando…" : "Enviar ahora"}
            </button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          <strong>{created.name}</strong> tiene <strong>{created.total.toLocaleString("es")}</strong> destinatarios.
          ¿Quieres enviarla ahora?
        </p>
        <ErrorBox error={error} />
      </Modal>
    );

  return (
    <Modal
      wide
      title="Nueva campaña"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancelar</button>
          <button className="primary" disabled={busy || !ready} onClick={create}>
            Crear campaña
          </button>
        </>
      }
    >
      <Field label="Nombre de la campaña">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Bonos octubre" autoFocus />
      </Field>

      {templates.loading && !templates.data ? (
        <Loading />
      ) : templates.error || usable.length === 0 ? (
        <div className="error-box">
          {templates.error
            ? `No se pudieron cargar las plantillas: ${templates.error}`
            : "No hay plantillas aprobadas disponibles."}{" "}
          Revisa que <code>WA_WABA_ID</code> y el token de acceso estén configurados en el backend y que tengas
          plantillas aprobadas en el WhatsApp Manager.
        </div>
      ) : (
        <Field label="Plantilla">
          <select value={tplKey} onChange={(e) => pickTemplate(e.target.value)}>
            <option value="">Selecciona una plantilla aprobada</option>
            {usable.map((t) => (
              <option key={`${t.name}|${t.language}`} value={`${t.name}|${t.language}`}>
                {t.name} ({t.language}) · {t.category}
              </option>
            ))}
          </select>
        </Field>
      )}

      {tpl && (
        <>
          <div className="card" style={{ background: "var(--out-campaign)" }}>
            <div className="inline small" style={{ marginBottom: 4 }}>
              <Badge tone={tpl.category === "MARKETING" ? "warn" : "info"}>{tpl.category}</Badge>
            </div>
            {tpl.header && <p className="strong" style={{ margin: "0 0 4px" }}>{tpl.header}</p>}
            <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{tpl.body}</p>
          </div>
          {tpl.variables.length > 0 && (
            <div className="grid2">
              {tpl.variables.map((v, i) => (
                <Field key={v} label={`Variable {{${v}}}`} hint={i === 0 ? "Usa {{nombre}} para personalizar con el nombre del cliente" : undefined}>
                  <input
                    value={values[i] ?? ""}
                    onChange={(e) => setValues(values.map((x, j) => (j === i ? e.target.value : x)))}
                  />
                </Field>
              ))}
            </div>
          )}
          {tpl.category === "MARKETING" && (
            <p className="small" style={{ margin: 0, color: "var(--warn)" }}>
              Plantilla de marketing: los contactos que pidieron no recibir marketing se omitirán automáticamente.
            </p>
          )}
        </>
      )}

      <Field label="Audiencia">
        <div className="inline">
          <label className="inline">
            <input type="radio" checked={audience === "tag"} onChange={() => setAudience("tag")} /> Por etiqueta
          </label>
          <label className="inline">
            <input type="radio" checked={audience === "segment"} onChange={() => setAudience("segment")} /> Por segmento
          </label>
          <label className="inline">
            <input type="radio" checked={audience === "phones"} onChange={() => setAudience("phones")} /> Lista de números
          </label>
        </div>
      </Field>
      {audience === "tag" ? (
        <select value={tag} onChange={(e) => setTag(e.target.value)}>
          <option value="">Selecciona una etiqueta</option>
          {tags.data?.map((t) => (
            <option key={t.tag} value={t.tag}>
              {t.tag} ({t.count} contactos)
            </option>
          ))}
        </select>
      ) : audience === "segment" ? (
        <select value={segmentId} onChange={(e) => setSegmentId(e.target.value)}>
          <option value="">Selecciona un segmento</option>
          {segments.data?.map((sg) => (
            <option key={sg.id} value={sg.id}>
              {sg.name} ({sg.member_count} clientes)
            </option>
          ))}
        </select>
      ) : (
        <Field label="Números" hint={`Uno por línea, con código de país. ${phoneList.length} números.`}>
          <textarea rows={6} value={phones} onChange={(e) => setPhones(e.target.value)} placeholder={"573001234567\n573109876543"} />
        </Field>
      )}
      <ErrorBox error={error} />
    </Modal>
  );
}
