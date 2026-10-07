"use client";

import { useEffect, useMemo, useState } from "react";
import { api, send, type Template } from "@/lib/api";
import { Badge, ErrorBox, Loading, useAction, useApi } from "@/components/ui";
import {
  CATEGORY_LABEL,
  TEMPLATE_STATUS,
  type PackTemplate,
  type TemplatePack,
  type TemplateSubmitResult,
} from "@/lib/onboarding-types";
import { StepFooter, renderTemplateText, useWizard } from "./common";

type Edit = { body?: string; header?: string; footer?: string };
type Submitted = TemplateSubmitResult["submitted"][number] & { rejected_reason?: string | null };

function Bubble({ t, edit }: { t: PackTemplate; edit?: Edit }) {
  const header = edit?.header ?? t.header;
  const footer = edit?.footer ?? t.footer;
  return (
    <div className="ob-chat" aria-label={`Vista previa de ${t.name}`}>
      <div className="ob-bubble">
        {header && <div className="strong">{renderTemplateText(header, t.example_values)}</div>}
        <div className="ob-bubble-body">{renderTemplateText(edit?.body ?? t.body, t.example_values)}</div>
        {footer && <div className="small muted">{footer}</div>}
        <div className="ob-bubble-time">10:24</div>
      </div>
      {t.buttons.length > 0 && (
        <div className="ob-bubble-buttons">
          {t.buttons.map((b, i) => (
            <span key={i} className="ob-bubble-button">
              {b.type === "URL" ? "🔗 " : b.type === "PHONE_NUMBER" ? "📞 " : "↩ "}
              {b.text}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TemplatesStep() {
  const { industry, next, goTo, reload, state } = useWizard();
  const pack = useApi<TemplatePack>(`/api/onboarding/template-pack?industry=${encodeURIComponent(industry ?? "otro")}`);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [editing, setEditing] = useState<string | null>(null);
  // Lo ya enviado en una visita anterior queda en el resultado del paso
  const [submitted, setSubmitted] = useState<Record<string, Submitted>>(() => {
    const prior = state.steps.find((s) => s.key === "templates")?.result?.submitted;
    return Array.isArray(prior) ? Object.fromEntries((prior as Submitted[]).filter((s) => s?.template_key).map((s) => [s.template_key, s])) : {};
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [live, setLive] = useState<Record<string, Template>>({});
  const [rewriting, setRewriting] = useState<string | null>(null);
  const [run, busy, error] = useAction();

  useEffect(() => {
    if (pack.data) setSelected(Object.fromEntries(pack.data.templates.map((t) => [t.template_key, t.recommended || !!submitted[t.template_key]])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pack.data]);

  const byName = useMemo(() => Object.fromEntries((pack.data?.templates ?? []).map((t) => [t.name, t.template_key])), [pack.data]);

  // Estado en vivo desde Meta (sincroniza mientras haya plantillas en revisión)
  const pendingCount = Object.values(submitted).filter((s) => {
    const st = (live[s.name]?.status ?? s.status ?? "").toUpperCase();
    return st === "PENDING" || st === "IN_APPEAL" || st === "SUBMITTED";
  }).length;
  useEffect(() => {
    if (!Object.keys(submitted).length) return;
    let alive = true;
    const tick = async () => {
      try {
        const list = await api<Template[]>(`/api/templates${pendingCount ? "?refresh=true" : ""}`);
        if (alive) setLive(Object.fromEntries(list.filter((t) => byName[t.name]).map((t) => [t.name, t])));
      } catch {}
    };
    void tick();
    if (!pendingCount) return () => void (alive = false);
    const timer = setInterval(tick, 10_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [submitted, pendingCount, byName]);

  function absorb(r: TemplateSubmitResult) {
    setSubmitted((prev) => ({ ...prev, ...Object.fromEntries(r.submitted.map((s) => [s.template_key, s])) }));
    setErrors((prev) => {
      const nextErr = { ...prev };
      for (const s of r.submitted) delete nextErr[s.template_key];
      for (const e of r.errors) nextErr[e.template_key] = e.message;
      return nextErr;
    });
  }

  async function submit() {
    const keys = Object.entries(selected)
      .filter(([k, v]) => v && !submitted[k])
      .map(([k]) => k);
    if (!keys.length) return;
    const r = await run(() => send<TemplateSubmitResult>("/api/onboarding/templates", "POST", { template_keys: keys, edits }));
    if (r) {
      absorb(r);
      await reload();
    }
  }

  async function rewrite(key: string) {
    setRewriting(key);
    const r = await run(() => send<TemplateSubmitResult>(`/api/onboarding/templates/${key}/rewrite`, "POST"));
    setRewriting(null);
    if (r) {
      absorb(r);
      await reload();
    }
  }

  if (!pack.data) return pack.error ? <ErrorBox error={pack.error} /> : <Loading />;
  const statusOf = (t: PackTemplate) => {
    const s = submitted[t.template_key];
    if (!s) return null;
    return (live[s.name]?.status ?? s.status ?? "SUBMITTED").toUpperCase();
  };
  const approved = pack.data.templates.filter((t) => statusOf(t) === "APPROVED").length;
  const anySubmitted = Object.keys(submitted).length > 0;
  const toSubmit = Object.entries(selected).filter(([k, v]) => v && !submitted[k]).length;

  return (
    <div className="ob-step">
      <section className="ob-hero-card">
        <h3>Plantillas recomendadas para tu industria</h3>
        <p className="small muted">
          Meta exige plantillas aprobadas para escribirle primero a un cliente o cuando pasaron más de 24 h desde su último mensaje. Preparamos estas con el nombre y el tono de tu empresa; edítalas si
          quieres y las enviamos a revisión (suele tardar de minutos a unas horas).
        </p>
        {anySubmitted && (
          <p className="small" aria-live="polite">
            {approved} aprobadas · {pendingCount} en revisión{pendingCount ? " · actualizando cada 10 s" : ""}
          </p>
        )}
      </section>

      <div className="ob-template-grid">
        {pack.data.templates.map((t) => {
          const st = statusOf(t);
          const meta = st ? TEMPLATE_STATUS[st] ?? { label: st, tone: "neutral" as const } : null;
          const sub = submitted[t.template_key];
          const reason = sub?.rejected_reason;
          const isEditing = editing === t.template_key;
          return (
            <article key={t.template_key} className={`ob-template ${selected[t.template_key] ? "selected" : ""}`}>
              <header className="ob-template-head">
                <label className="inline">
                  <input
                    type="checkbox"
                    checked={!!selected[t.template_key]}
                    disabled={!!sub}
                    onChange={(e) => setSelected({ ...selected, [t.template_key]: e.target.checked })}
                  />
                  <span className="strong">{t.name}</span>
                </label>
                <span className="inline">
                  <Badge tone="neutral">{CATEGORY_LABEL[t.category] ?? t.category}</Badge>
                  {t.recommended && !meta && <Badge tone="info">Recomendada</Badge>}
                  {meta && <Badge tone={meta.tone}>{meta.label}</Badge>}
                </span>
              </header>
              <Bubble t={t} edit={edits[t.template_key]} />
              {isEditing && !sub && (
                <div className="ob-template-edit">
                  <label className="field">
                    <span>Texto</span>
                    <textarea
                      rows={5}
                      value={edits[t.template_key]?.body ?? t.body}
                      onChange={(e) => setEdits({ ...edits, [t.template_key]: { ...edits[t.template_key], body: e.target.value } })}
                    />
                    <small className="muted">Conserva las variables entre llaves dobles, por ejemplo {"{{1}}"}.</small>
                  </label>
                </div>
              )}
              {errors[t.template_key] && <p className="error-box small">{errors[t.template_key]}</p>}
              {st === "REJECTED" && <p className="small ob-note warn">Meta la rechazó{reason ? `: ${reason}` : "."} La IA puede reescribirla siguiendo las políticas.</p>}
              <footer className="inline" style={{ justifyContent: "flex-end" }}>
                {!sub && (
                  <button className="link small" onClick={() => setEditing(isEditing ? null : t.template_key)}>
                    {isEditing ? "Listo" : "Editar"}
                  </button>
                )}
                {(st === "REJECTED" || errors[t.template_key]) && (
                  <button className="small" onClick={() => rewrite(t.template_key)} disabled={rewriting === t.template_key}>
                    {rewriting === t.template_key ? "Reescribiendo…" : "✨ Reescribir con IA"}
                  </button>
                )}
              </footer>
            </article>
          );
        })}
      </div>
      <ErrorBox error={error} />
      <StepFooter
        onBack={() => goTo("profile")}
        onNext={async () => {
          if (toSubmit) await submit();
          else next();
        }}
        busy={busy && !rewriting}
        nextLabel={toSubmit ? `Enviar ${toSubmit} a revisión de Meta` : "Continuar"}
        nextDisabled={!toSubmit && !anySubmitted}
        extra={
          anySubmitted && toSubmit ? (
            <button onClick={next} disabled={busy}>
              Continuar sin enviar más
            </button>
          ) : null
        }
      />
    </div>
  );
}
