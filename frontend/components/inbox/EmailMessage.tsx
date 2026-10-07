"use client";

import { useEffect, useState } from "react";
import { api, downloadUrl } from "@/lib/api";

type EmailDetail = {
  id: number;
  subject: string | null;
  from: string | null;
  from_name: string | null;
  to: string[];
  cc: string[];
  date: string | null;
  text: string | null;
  html: string | null;
  attachments: { index: number; filename: string | null; mime: string | null; size: number | null }[];
};

const kb = (n: number | null) => (n == null ? "" : n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`);

/** Correo dentro de la burbuja: asunto, remitente, cuerpo HTML (ya limpio en el servidor, además aislado en un
 * iframe sin scripts) y adjuntos. Uso: en Chat.tsx → Bubble, `{m.type === "email" && <EmailMessage messageId={m.id} />}`. */
export default function EmailMessage({ messageId, fallback }: { messageId: number; fallback?: string | null }) {
  const [mail, setMail] = useState<EmailDetail | null>(null);
  const [showHtml, setShowHtml] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api<EmailDetail>(`/api/messages/${messageId}/email`).then(setMail).catch(() => setFailed(true));
  }, [messageId]);

  if (failed) return fallback ? <p>{fallback}</p> : null;  // sin detalle del correo: al menos el texto
  if (!mail) return <div className="muted small">Cargando correo…</div>;
  return (
    <div className="email-msg" style={{ display: "grid", gap: 4, minWidth: 0 }}>
      <div className="small">
        <strong>{mail.subject || "(sin asunto)"}</strong>
        <div className="muted">
          {mail.from_name ? `${mail.from_name} <${mail.from}>` : mail.from}
          {mail.cc.length > 0 && <> · CC: {mail.cc.join(", ")}</>}
        </div>
      </div>
      {mail.html && (
        <>
          <button type="button" className="link small" style={{ justifySelf: "start" }} onClick={() => setShowHtml((v) => !v)}
            aria-expanded={showHtml}>
            {showHtml ? "Ver solo el texto" : "Ver correo con formato"}
          </button>
          {showHtml && (
            <iframe
              title={`Correo: ${mail.subject ?? ""}`}
              sandbox=""
              srcDoc={`<!doctype html><meta charset="utf-8"><base target="_blank"><style>body{font:14px/1.45 system-ui,sans-serif;margin:8px;color:#1f2937}img{max-width:100%}</style>${mail.html}`}
              style={{ width: "100%", minHeight: 220, border: "1px solid var(--border)", borderRadius: 8, background: "#fff" }}
            />
          )}
        </>
      )}
      {mail.attachments.length > 0 && (
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          {mail.attachments.map((a) => (
            <li key={a.index}>
              <a href={downloadUrl(`/api/messages/${mail.id}/email/attachments/${a.index}`)} target="_blank" rel="noreferrer">
                📎 {a.filename || "adjunto"}
              </a>{" "}
              <span className="muted">{kb(a.size)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
