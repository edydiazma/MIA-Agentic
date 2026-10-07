"use client";

import { send } from "@/lib/api";
import { ErrorBox, Field, useAction } from "@/components/ui";
import type { Answers } from "@/lib/onboarding-types";
import { StepFooter, useWizard } from "./common";

/** Verticales que acepta Meta para el perfil de empresa. */
const VERTICALS: [string, string][] = [
  ["AUTO", "Automotriz"],
  ["HEALTH", "Salud"],
  ["EDU", "Educación"],
  ["RETAIL", "Retail"],
  ["APPAREL", "Ropa y moda"],
  ["BEAUTY", "Belleza, spa y salón"],
  ["FINANCE", "Finanzas y banca"],
  ["PROF_SERVICES", "Servicios profesionales"],
  ["TRAVEL", "Viajes y transporte"],
  ["RESTAURANT", "Restaurante"],
  ["HOTEL", "Hotel y alojamiento"],
  ["GROCERY", "Supermercado"],
  ["ENTERTAIN", "Entretenimiento"],
  ["EVENT_PLAN", "Eventos"],
  ["GOVT", "Gobierno"],
  ["NONPROFIT", "Sin ánimo de lucro"],
  ["OTHER", "Otro"],
];
const INDUSTRY_VERTICAL: Record<string, string> = {
  automotriz: "AUTO",
  salud: "HEALTH",
  educacion: "EDU",
  retail: "RETAIL",
  servicios: "PROF_SERVICES",
  inmobiliaria: "OTHER",
  otro: "OTHER",
};

type Profile = NonNullable<Answers["profile"]>;

export default function ProfileStep() {
  const { answers, update, industry, state, next, goTo, flush, reload } = useWizard();
  const company = answers.company ?? {};
  const p: Profile = {
    about: answers.profile?.about ?? (company.about || company.description || "").slice(0, 139),
    description: answers.profile?.description ?? company.description ?? "",
    address: answers.profile?.address ?? company.address ?? "",
    email: answers.profile?.email ?? company.email ?? "",
    websites: answers.profile?.websites ?? (company.website ? [company.website] : []),
    vertical: answers.profile?.vertical ?? INDUSTRY_VERTICAL[industry ?? "otro"] ?? "OTHER",
  };
  const set = (patch: Partial<Profile>) => update((a) => ({ ...a, profile: { ...p, ...(a.profile ?? {}), ...patch } }));
  const [run, busy, error] = useAction();
  const step = state.steps.find((s) => s.key === "profile");
  const name = company.name || state.org.name;

  async function publish() {
    // Fija los valores mostrados (aunque vengan de la empresa) antes de publicarlos
    update((a) => ({ ...a, profile: { ...p, ...(a.profile ?? {}) } }));
    await flush();
    const ok = await run(() => send("/api/onboarding/steps/profile/run", "POST"));
    if (ok !== undefined) {
      await reload();
      next();
    }
  }

  return (
    <div className="ob-step">
      <div className="ob-two-col">
        <section className="card">
          <h3>Perfil de empresa en WhatsApp</h3>
          <Field label={`Frase corta (${(p.about ?? "").length}/139)`} hint="Aparece debajo de tu nombre.">
            <input value={p.about} maxLength={139} onChange={(e) => set({ about: e.target.value })} />
          </Field>
          <Field label={`Descripción (${(p.description ?? "").length}/512)`}>
            <textarea rows={4} maxLength={512} value={p.description} onChange={(e) => set({ description: e.target.value })} />
          </Field>
          <Field label="Dirección">
            <input value={p.address} maxLength={256} onChange={(e) => set({ address: e.target.value })} />
          </Field>
          <Field label="Correo">
            <input type="email" value={p.email} maxLength={128} onChange={(e) => set({ email: e.target.value })} />
          </Field>
          <Field label="Sitios web (máximo 2)">
            <input
              value={(p.websites ?? []).join(", ")}
              onChange={(e) => set({ websites: e.target.value.split(",").map((w) => w.trim()).filter(Boolean).slice(0, 2) })}
              placeholder="https://www.tuempresa.com"
            />
          </Field>
          <Field label="Categoría">
            <select value={p.vertical} onChange={(e) => set({ vertical: e.target.value })}>
              {VERTICALS.map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
          <ErrorBox error={error || step?.error} />
        </section>

        <aside className="ob-phone" aria-label="Vista previa del perfil">
          <div className="ob-phone-screen">
            <div className="ob-wa-profile">
              <div className="ob-avatar" aria-hidden>
                {(name ?? "?").slice(0, 1).toUpperCase()}
              </div>
              <div className="ob-wa-name">
                {name} <span className="ob-verified" title="Cuenta de empresa">✓</span>
              </div>
              <div className="small muted">Cuenta de empresa</div>
              {p.about && <div className="ob-wa-about">{p.about}</div>}
            </div>
            <dl className="ob-wa-fields">
              {p.description && (
                <>
                  <dt>Descripción</dt>
                  <dd>{p.description}</dd>
                </>
              )}
              {p.address && (
                <>
                  <dt>📍 Dirección</dt>
                  <dd>{p.address}</dd>
                </>
              )}
              {p.email && (
                <>
                  <dt>✉️ Correo</dt>
                  <dd>{p.email}</dd>
                </>
              )}
              {(p.websites ?? []).map((w) => (
                <div key={w}>
                  <dt>🔗 Sitio web</dt>
                  <dd>{w}</dd>
                </div>
              ))}
            </dl>
          </div>
        </aside>
      </div>
      <StepFooter
        onBack={() => goTo("validate")}
        onNext={publish}
        busy={busy}
        nextLabel="Publicar perfil y continuar"
        extra={
          <button className="link" onClick={() => send("/api/onboarding/steps/profile/skip", "POST").then(reload).then(next, next)}>
            Omitir
          </button>
        }
      />
    </div>
  );
}
