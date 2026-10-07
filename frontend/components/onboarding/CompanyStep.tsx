"use client";

import { useState } from "react";
import { send } from "@/lib/api";
import { ErrorBox, Field, Toggle, useAction } from "@/components/ui";
import {
  COUNTRIES,
  DAYS,
  DAY_LABEL,
  INDUSTRIES,
  TONES,
  type CompanyAnswers,
  type Day,
  type DayHours,
  type Faq,
  type Product,
  type WebsiteImport,
} from "@/lib/onboarding-types";
import { StepFooter, useWizard } from "./common";

const DEFAULT_HOURS: Record<Day, DayHours> = {
  mon: { open: true, from: "08:00", to: "18:00" },
  tue: { open: true, from: "08:00", to: "18:00" },
  wed: { open: true, from: "08:00", to: "18:00" },
  thu: { open: true, from: "08:00", to: "18:00" },
  fri: { open: true, from: "08:00", to: "18:00" },
  sat: { open: true, from: "09:00", to: "13:00" },
  sun: { open: false, from: "09:00", to: "13:00" },
};

const PROFILE_FIELDS: { key: keyof CompanyAnswers; label: string }[] = [
  { key: "name", label: "Nombre" },
  { key: "description", label: "Descripción" },
  { key: "about", label: "Frase corta" },
  { key: "address", label: "Dirección" },
  { key: "email", label: "Correo" },
  { key: "phone", label: "Teléfono" },
];

export default function CompanyStep() {
  const { answers, update, industry, setIndustry, next, state, flush, reload } = useWizard();
  const company = answers.company ?? {};
  const hours = { ...DEFAULT_HOURS, ...(company.hours ?? {}) } as Record<Day, DayHours>;
  const [url, setUrl] = useState(answers.import?.url ?? company.website ?? "");
  const [imported, setImported] = useState<WebsiteImport | null>(null);
  const [keepProfile, setKeepProfile] = useState<Record<string, boolean>>({});
  const [keepFaq, setKeepFaq] = useState<boolean[]>([]);
  const [keepProduct, setKeepProduct] = useState<boolean[]>([]);
  const [run, busy, error] = useAction();
  const [touched, setTouched] = useState(false);

  const setCompany = (patch: Partial<CompanyAnswers>) => update((a) => ({ ...a, company: { ...(a.company ?? {}), ...patch } }));
  const setDay = (d: Day, patch: Partial<DayHours>) => setCompany({ hours: { ...hours, [d]: { ...hours[d], ...patch } } });

  async function importSite() {
    const target = url.trim();
    if (!target) return;
    const r = await run(() => send<WebsiteImport>("/api/onboarding/import-website", "POST", { url: /^https?:\/\//.test(target) ? target : `https://${target}` }));
    if (!r) return;
    setImported(r);
    setKeepProfile(Object.fromEntries(PROFILE_FIELDS.map((f) => [f.key, !!r.profile[f.key]])));
    setKeepFaq(r.faqs.map(() => true));
    setKeepProduct(r.products.map(() => true));
  }

  function applyImport() {
    if (!imported) return;
    const p = imported.profile;
    const picked: Partial<CompanyAnswers> = { website: url.trim() };
    for (const f of PROFILE_FIELDS) if (keepProfile[f.key] && p[f.key]) (picked as Record<string, unknown>)[f.key] = p[f.key];
    if (p.tone && !company.tone) picked.tone = String(p.tone);
    const faqs: Faq[] = imported.faqs.filter((_, i) => keepFaq[i]);
    const products: Product[] = imported.products.filter((_, i) => keepProduct[i]);
    update((a) => ({
      ...a,
      company: { ...(a.company ?? {}), ...picked },
      import: { url: url.trim(), faqs, products, pages_read: imported.pages_read },
    }));
    setImported(null);
  }

  const name = (company.name ?? state.org.name ?? "").trim();
  const missing = !name ? "Escribe el nombre de tu empresa" : !industry ? "Elige tu industria" : null;

  return (
    <div className="ob-step">
      <section className="ob-hero-card">
        <div>
          <h3>Empieza con tu sitio web</h3>
          <p className="muted small">Leemos tu sitio y completamos el perfil, las preguntas frecuentes y tus productos. Tú decides qué conservar.</p>
        </div>
        <div className="ob-import-row">
          <label className="sr-only" htmlFor="ob-url">Sitio web</label>
          <input id="ob-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.tuempresa.com" inputMode="url" />
          <button className="primary" onClick={importSite} disabled={busy || !url.trim()}>
            {busy ? "Leyendo tu sitio…" : "Importar desde mi sitio"}
          </button>
        </div>
        {busy && <div className="ob-shimmer" aria-hidden />}
        <ErrorBox error={error} />
        {answers.import?.pages_read ? (
          <p className="small muted">
            ✓ Importado de {answers.import.url}: {answers.import.pages_read} páginas, {answers.import.faqs?.length ?? 0} preguntas frecuentes y{" "}
            {answers.import.products?.length ?? 0} productos.
          </p>
        ) : null}
      </section>

      {imported && (
        <section className="card ob-import-result" aria-live="polite">
          <h3>Esto encontramos en {imported.pages_read} páginas</h3>
          <div className="grid2">
            <div>
              <div className="ob-subtitle">Perfil</div>
              {PROFILE_FIELDS.filter((f) => imported.profile[f.key]).map((f) => (
                <label key={f.key} className="ob-keep">
                  <input type="checkbox" checked={!!keepProfile[f.key]} onChange={(e) => setKeepProfile({ ...keepProfile, [f.key]: e.target.checked })} />
                  <span>
                    <span className="small muted">{f.label}</span>
                    <br />
                    {String(imported.profile[f.key])}
                  </span>
                </label>
              ))}
            </div>
            <div>
              <div className="ob-subtitle">Preguntas frecuentes ({imported.faqs.length})</div>
              {imported.faqs.map((f, i) => (
                <label key={i} className="ob-keep">
                  <input type="checkbox" checked={!!keepFaq[i]} onChange={(e) => setKeepFaq(keepFaq.map((v, j) => (j === i ? e.target.checked : v)))} />
                  <span>
                    <span className="strong">{f.question}</span>
                    <br />
                    <span className="small muted">{f.answer}</span>
                  </span>
                </label>
              ))}
              {imported.products.length > 0 && <div className="ob-subtitle">Productos ({imported.products.length})</div>}
              {imported.products.map((p, i) => (
                <label key={i} className="ob-keep">
                  <input type="checkbox" checked={!!keepProduct[i]} onChange={(e) => setKeepProduct(keepProduct.map((v, j) => (j === i ? e.target.checked : v)))} />
                  <span>
                    <span className="strong">{p.name}</span>
                    {p.price != null && <span className="small muted"> · {p.price} {p.currency ?? ""}</span>}
                    {p.description && (
                      <>
                        <br />
                        <span className="small muted">{p.description}</span>
                      </>
                    )}
                  </span>
                </label>
              ))}
            </div>
          </div>
          <div className="inline" style={{ justifyContent: "flex-end" }}>
            <button onClick={() => setImported(null)}>Descartar</button>
            <button className="primary" onClick={applyImport}>
              Usar lo seleccionado
            </button>
          </div>
        </section>
      )}

      <section className="card">
        <h3>Datos de la empresa</h3>
        <div className="grid2">
          <Field label="Nombre de la empresa">
            <input value={company.name ?? state.org.name ?? ""} onChange={(e) => setCompany({ name: e.target.value })} onBlur={() => setTouched(true)} required />
          </Field>
          <Field label="Sitio web">
            <input value={company.website ?? ""} onChange={(e) => setCompany({ website: e.target.value })} inputMode="url" />
          </Field>
          <Field label="País">
            <select
              value={company.country ?? ""}
              onChange={(e) => {
                const c = COUNTRIES.find((x) => x.code === e.target.value);
                setCompany({ country: e.target.value, timezone: c?.tz ?? company.timezone });
              }}
            >
              <option value="">Elige…</option>
              {COUNTRIES.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Zona horaria" hint="Se usa para horarios de atención, reportes y recordatorios.">
            <input value={company.timezone ?? ""} onChange={(e) => setCompany({ timezone: e.target.value })} placeholder="America/Bogota" list="ob-tz" />
            <datalist id="ob-tz">
              {COUNTRIES.map((c) => (
                <option key={c.code} value={c.tz} />
              ))}
            </datalist>
          </Field>
        </div>
        <Field label="¿Qué hace tu empresa?" hint="Una o dos frases. La IA la usa para presentarse y responder.">
          <textarea rows={3} value={company.description ?? ""} onChange={(e) => setCompany({ description: e.target.value })} />
        </Field>
        <div className="grid2">
          <Field label="Dirección">
            <input value={company.address ?? ""} onChange={(e) => setCompany({ address: e.target.value })} />
          </Field>
          <Field label="Correo de contacto">
            <input type="email" value={company.email ?? ""} onChange={(e) => setCompany({ email: e.target.value })} />
          </Field>
        </div>
      </section>

      <section className="card">
        <h3 id="ob-industry">Industria</h3>
        <p className="small muted">Con esto elegimos plantillas, tipificaciones, grupos y el tono de la IA.</p>
        <div className="ob-cards" role="radiogroup" aria-labelledby="ob-industry">
          {INDUSTRIES.map((i) => (
            <button
              key={i.key}
              role="radio"
              aria-checked={industry === i.key}
              className={`ob-choice ${industry === i.key ? "selected" : ""}`}
              onClick={() => setIndustry(i.key)}
            >
              <span className="ob-choice-icon" aria-hidden>
                {i.icon}
              </span>
              <span className="strong">{i.label}</span>
              <span className="small muted">{i.hint}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="card">
        <h3>Horario de atención</h3>
        <p className="small muted">Fuera de este horario la IA atiende y avisa cuándo responderá un asesor.</p>
        <div className="ob-hours">
          {DAYS.map((d) => (
            <div key={d} className="ob-hours-row">
              <Toggle checked={hours[d].open} onChange={(v) => setDay(d, { open: v })} label={DAY_LABEL[d]} />
              {hours[d].open ? (
                <span className="inline">
                  <input type="time" aria-label={`${DAY_LABEL[d]} desde`} value={hours[d].from} onChange={(e) => setDay(d, { from: e.target.value })} />
                  <span className="muted">a</span>
                  <input type="time" aria-label={`${DAY_LABEL[d]} hasta`} value={hours[d].to} onChange={(e) => setDay(d, { to: e.target.value })} />
                </span>
              ) : (
                <span className="muted small">Cerrado</span>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <h3 id="ob-tone">Tono de marca</h3>
        <div className="chips" role="radiogroup" aria-labelledby="ob-tone">
          {TONES.map((t) => (
            <button key={t.key} role="radio" aria-checked={company.tone === t.key} className={`chip ${company.tone === t.key ? "active" : ""}`} onClick={() => setCompany({ tone: t.key })}>
              {t.label}
            </button>
          ))}
        </div>
      </section>

      {touched && missing && <p className="error-box">{missing}</p>}
      <StepFooter
        onNext={async () => {
          setTouched(true);
          if (missing) return;
          await flush();
          // Aplica perfil, horarios y zona horaria a la configuración de la empresa
          const ok = await run(() => send("/api/onboarding/steps/company/run", "POST"));
          if (ok !== undefined) {
            await reload();
            next();
          }
        }}
        nextDisabled={!!missing && touched}
        busy={busy}
        nextLabel="Guardar y continuar"
      />
    </div>
  );
}
