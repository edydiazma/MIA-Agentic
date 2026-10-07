"use client";

import { send } from "@/lib/api";
import { ErrorBox, Field, Toggle, useAction } from "@/components/ui";
import { INDUSTRIES, TONES, type Answers } from "@/lib/onboarding-types";
import { StepFooter, useWizard } from "./common";

/** Sugerencias por industria (el servidor aplica las suyas; esto es la vista previa). */
const TYPIFICATIONS: Record<string, string[]> = {
  automotriz: ["Venta", "Cotización enviada", "Prueba de manejo agendada", "Cita de taller", "Repuestos", "Postventa", "Sin respuesta"],
  salud: ["Cita agendada", "Consulta resuelta", "Resultados", "Reprogramación", "Facturación", "Sin respuesta"],
  educacion: ["Inscripción", "Información enviada", "Visita agendada", "Pagos", "Soporte académico", "Sin respuesta"],
  retail: ["Venta", "Estado de pedido", "Cambio o devolución", "Información de producto", "Reclamo", "Sin respuesta"],
  servicios: ["Venta", "Cotización enviada", "Soporte resuelto", "Reclamo", "Agendamiento", "Sin respuesta"],
  inmobiliaria: ["Visita agendada", "Información enviada", "Negocio cerrado", "Financiación", "Arriendos", "Sin respuesta"],
  otro: ["Venta", "Consulta resuelta", "Cotización enviada", "Reclamo", "Sin respuesta"],
};
const GROUPS: Record<string, string[]> = {
  automotriz: ["Ventas", "Taller", "Repuestos"],
  salud: ["Citas", "Atención al paciente"],
  educacion: ["Admisiones", "Soporte"],
  retail: ["Ventas", "Servicio al cliente"],
  servicios: ["Comercial", "Soporte"],
  inmobiliaria: ["Ventas", "Arriendos"],
  otro: ["Ventas", "Soporte"],
};

type AI = NonNullable<Answers["ai"]>;
const ITEMS: { key: keyof AI; title: string }[] = [
  { key: "apply_prompt", title: "Agente de IA con el perfil y el tono de tu empresa" },
  { key: "apply_knowledge", title: "Base de conocimiento con tus preguntas frecuentes" },
  { key: "apply_catalog", title: "Catálogo con los productos importados" },
  { key: "apply_typifications", title: "Tipificaciones para clasificar conversaciones" },
  { key: "apply_groups", title: "Grupos de atención para enrutar a tus asesores" },
  { key: "apply_hours_message", title: "Mensaje fuera de horario" },
];

export default function AIStep() {
  const { answers, update, industry, state, next, goTo, flush, reload } = useWizard();
  const company = answers.company ?? {};
  const faqs = answers.import?.faqs ?? [];
  const products = answers.import?.products ?? [];
  const ai: AI = {
    apply_prompt: true,
    apply_knowledge: faqs.length > 0,
    apply_catalog: products.length > 0,
    apply_typifications: true,
    apply_groups: true,
    apply_hours_message: true,
    hours_message: `¡Hola! Gracias por escribir a ${company.name || state.org.name}. Nuestro equipo atiende en horario laboral; te respondemos apenas abramos. Mientras tanto, nuestro asistente puede ayudarte.`,
    ...(answers.ai ?? {}),
  };
  const set = (patch: Partial<AI>) => update((a) => ({ ...a, ai: { ...ai, ...(a.ai ?? {}), ...patch } }));
  const [run, busy, error] = useAction();
  const step = state.steps.find((s) => s.key === "ai");
  const ind = industry ?? "otro";
  const tone = TONES.find((t) => t.key === company.tone)?.label ?? "Cercano y amable";
  const industryLabel = INDUSTRIES.find((i) => i.key === ind)?.label ?? "tu industria";

  const details: Record<string, React.ReactNode> = {
    apply_prompt: (
      <div className="ob-prompt-preview">
        <p className="small">
          «Eres el asistente virtual de <strong>{company.name || state.org.name}</strong>
          {company.description ? `, ${company.description.replace(/\.$/, "")}` : ""}. Respondes por WhatsApp en tono <strong>{tone.toLowerCase()}</strong>, das información
          precisa de {industryLabel.toLowerCase()} y pasas la conversación a un asesor cuando el cliente lo pide o quiere comprar.»
        </p>
      </div>
    ),
    apply_knowledge: <span className="small muted">{faqs.length ? `${faqs.length} preguntas frecuentes importadas de tu sitio.` : "No importaste preguntas frecuentes (paso «Tu empresa»)."}</span>,
    apply_catalog: <span className="small muted">{products.length ? `${products.length} productos importados.` : "No importaste productos."}</span>,
    apply_typifications: <span className="small muted">{(TYPIFICATIONS[ind] ?? TYPIFICATIONS.otro).join(" · ")}</span>,
    apply_groups: <span className="small muted">{(GROUPS[ind] ?? GROUPS.otro).join(" · ")}</span>,
    apply_hours_message: (
      <Field label="Mensaje">
        <textarea rows={3} value={ai.hours_message} onChange={(e) => set({ hours_message: e.target.value })} disabled={!ai.apply_hours_message} />
      </Field>
    ),
  };

  async function apply() {
    update((a) => ({ ...a, ai: { ...ai, ...(a.ai ?? {}) } }));
    await flush();
    const ok = await run(() => send("/api/onboarding/steps/ai/run", "POST"));
    if (ok !== undefined) {
      await reload();
      next();
    }
  }

  const result = step?.status === "done" || step?.status === "warning" ? step.result : null;

  return (
    <div className="ob-step">
      <section className="ob-hero-card">
        <h3>Dejamos todo configurado</h3>
        <p className="small muted">Revisa lo que vamos a crear. Puedes ajustar cualquier cosa después en Configuraciones y en Cortex.</p>
      </section>
      <section className="card">
        <ul className="ob-apply-list">
          {ITEMS.map((it) => (
            <li key={it.key}>
              <Toggle checked={!!ai[it.key]} onChange={(v) => set({ [it.key]: v } as Partial<AI>)} label={it.title} />
              <div className="ob-apply-detail">{details[it.key as string]}</div>
            </li>
          ))}
        </ul>
        <ErrorBox error={error || step?.error} />
        {result && Object.keys(result).length > 0 && (
          <div className="ob-note small" role="status">
            ✓ Aplicado:{" "}
            {Object.entries(result)
              .filter(([, v]) => typeof v === "number" || typeof v === "string" || typeof v === "boolean")
              .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`)
              .join(" · ")}
          </div>
        )}
      </section>
      <StepFooter onBack={() => goTo("templates")} onNext={apply} busy={busy} nextLabel={result ? "Aplicar de nuevo y continuar" : "Aplicar y continuar"} />
    </div>
  );
}
