"use client";

import Link from "next/link";
import {
  ApiError,
  SENTIMENT_LABEL,
  send,
  timeAgo,
  type ClassifyResponse,
  type Conversation,
} from "@/lib/api";
import { useMe } from "@/components/Shell";
import { Badge, useAction } from "@/components/ui";

const pct = (n: number) => `${Math.round(n * 100)} %`;

function fmtValue(v: string | number | boolean) {
  if (typeof v === "boolean") return v ? "Sí" : "No";
  return String(v);
}

/** Resumen, sentimiento y sugerencias de la IA para la conversación, con aceptar / descartar. */
export default function AIAnalysis({
  conversation: c,
  onConversation,
}: {
  conversation: Conversation;
  onConversation: (c: Conversation) => void;
}) {
  const me = useMe();
  const [run, busy, error, setError] = useAction();
  const sug = c.ai_suggestions ?? {};
  const hasSuggestions = !!(sug.tags?.length || sug.typification || sug.group || sug.fields?.length);

  async function analyze() {
    const r = await run(async () => {
      try {
        return await send<ClassifyResponse>(`/api/conversations/${c.id}/classify`, "POST", { apply: true });
      } catch (e) {
        // 409 = la clasificación está desactivada: se muestra un aviso en lugar del error crudo
        if (e instanceof ApiError && e.status === 409) throw new Error("disabled");
        throw e;
      }
    });
    if (r?.conversation) onConversation(r.conversation);
    else if (r?.skipped) setError(r.skipped);
  }

  async function act(action: "accept" | "dismiss", kind: "tags" | "typification" | "group" | "field", key?: string) {
    const updated = await run(() =>
      send<Conversation>(`/api/conversations/${c.id}/suggestions/${action}`, "POST", { kind, key }),
    );
    if (updated) onConversation(updated);
  }

  const Actions = ({ kind, k }: { kind: "tags" | "typification" | "group" | "field"; k?: string }) => (
    <span className="inline" style={{ gap: 4 }}>
      <button className="small" disabled={busy} onClick={() => act("accept", kind, k)} title="Aceptar">
        ✓ Aceptar
      </button>
      <button className="small icon" disabled={busy} onClick={() => act("dismiss", kind, k)} title="Descartar">
        ✕
      </button>
    </span>
  );

  return (
    <div>
      <div className="row">
        <h3 style={{ margin: 0 }}>Análisis IA</h3>
        <button className="small" disabled={busy} onClick={analyze}>
          {busy ? "Analizando…" : "✨ Analizar"}
        </button>
      </div>

      {error === "disabled" ? (
        <p className="small muted" style={{ margin: "6px 0 0" }}>
          La clasificación con IA está desactivada.{" "}
          {me?.role === "admin" && <Link href="/configuraciones/clasificacion">Configurarla</Link>}
        </p>
      ) : (
        error && <div className="error-box small">{error}</div>
      )}

      {!c.ai_classified_at && !error && (
        <p className="small muted" style={{ margin: "6px 0 0" }}>
          Aún no se ha analizado esta conversación.
        </p>
      )}

      {c.ai_classified_at && (
        <div className="stack small" style={{ gap: 6, marginTop: 6 }}>
          <div className="inline" style={{ gap: 6 }}>
            {c.ai_sentiment && (
              <Badge tone={c.ai_sentiment === "negative" ? "bad" : c.ai_sentiment === "positive" ? "ok" : "neutral"}>
                {SENTIMENT_LABEL[c.ai_sentiment]}
              </Badge>
            )}
            <span className="muted">Analizado {timeAgo(c.ai_classified_at)}</span>
          </div>
          {c.ai_summary && <p style={{ margin: 0 }}>{c.ai_summary}</p>}
        </div>
      )}

      {hasSuggestions && (
        <div className="stack small" style={{ gap: 8, marginTop: 10 }}>
          <strong>Sugerencias</strong>
          {sug.tags && sug.tags.length > 0 && (
            <div className="row">
              <span>
                Etiquetas:{" "}
                {sug.tags.map((t) => (
                  <span key={t} className="tag">
                    {t}
                  </span>
                ))}
              </span>
              <Actions kind="tags" />
            </div>
          )}
          {sug.typification && (
            <div className="row">
              <span>
                Tipificación: <strong>{sug.typification.value}</strong>{" "}
                <span className="muted">({pct(sug.typification.confidence)})</span>
              </span>
              <Actions kind="typification" />
            </div>
          )}
          {sug.group && (
            <div className="row">
              <span>
                Grupo: <strong>{sug.group.value}</strong> <span className="muted">({pct(sug.group.confidence)})</span>
              </span>
              <Actions kind="group" />
            </div>
          )}
          {(sug.fields ?? []).map((f) => (
            <div key={f.key} className="row" style={{ alignItems: "flex-start" }}>
              <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                {f.label}: <strong>{fmtValue(f.value)}</strong> <span className="muted">({pct(f.confidence)})</span>
                {f.evidence && <div className="muted" style={{ fontStyle: "italic" }}>“{f.evidence}”</div>}
              </span>
              <Actions kind="field" k={f.key} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
