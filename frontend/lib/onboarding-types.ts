/** Asistente de onboarding (docs/data-model.md §13). Contrato de /api/onboarding. */

/** sessionStorage: el administrador salió del asistente; no redirigir otra vez en esta sesión. */
export const ONBOARDING_SKIP_KEY = "onboarding_skip_redirect";

export type StepKey = "company" | "whatsapp" | "validate" | "profile" | "templates" | "ai" | "team" | "test" | "go_live";
export type StepStatus = "pending" | "running" | "done" | "warning" | "failed" | "skipped";
export type CheckStatus = "pass" | "warn" | "fail" | "pending" | "skipped";

export const STEP_ORDER: StepKey[] = ["company", "whatsapp", "validate", "profile", "templates", "ai", "team", "test", "go_live"];
export const REQUIRED_STEPS: StepKey[] = ["company", "whatsapp", "validate", "templates", "go_live"];
/** Pasos que necesitan el número ya conectado. */
export const NEEDS_CHANNEL: StepKey[] = ["validate", "profile", "templates", "test", "go_live"];

export const STEP_FALLBACK: Record<StepKey, { label: string; description: string }> = {
  company: { label: "Tu empresa", description: "Perfil, industria, horarios e importación desde tu sitio web" },
  whatsapp: { label: "Conectar WhatsApp", description: "Vincula tu número con Meta en un par de minutos" },
  validate: { label: "Validación", description: "Revisamos registro, calidad, límites y webhook" },
  profile: { label: "Perfil de WhatsApp", description: "Lo que ven tus clientes al abrir tu chat" },
  templates: { label: "Plantillas", description: "Mensajes aprobados por Meta para escribir primero" },
  ai: { label: "IA y ajustes", description: "Agente de IA, conocimiento, tipificaciones y grupos" },
  team: { label: "Equipo", description: "Invita a tus asesores y supervisores" },
  test: { label: "Prueba", description: "Envía y recibe un mensaje real" },
  go_live: { label: "Salir en vivo", description: "Revisión final y activación" },
};

export type OnboardingRun = {
  id: number;
  status: "in_progress" | "completed" | "abandoned";
  current_step: StepKey;
  industry: string | null;
  answers: Answers;
  channel_id: number | null;
  started_at: string;
  completed_at: string | null;
};

export type OnboardingStep = {
  key: StepKey;
  label: string;
  description: string;
  status: StepStatus;
  attempts: number;
  result: Record<string, unknown>;
  error: string | null;
  required: boolean;
};

export type HealthCheck = {
  check_key: string;
  label: string;
  status: CheckStatus;
  detail: string | null;
  fixable: boolean;
  checked_at: string | null;
};

export type OnboardingState = {
  run: OnboardingRun | null;
  steps: OnboardingStep[];
  checks: HealthCheck[];
  org: { name: string; industry: string | null; onboarding_completed_at: string | null };
  embedded_signup: { enabled: boolean; app_id: string | null; config_id: string | null; api_version: string };
};

export type DayHours = { open: boolean; from: string; to: string };
export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Day = (typeof DAYS)[number];
export const DAY_LABEL: Record<Day, string> = {
  mon: "Lunes",
  tue: "Martes",
  wed: "Miércoles",
  thu: "Jueves",
  fri: "Viernes",
  sat: "Sábado",
  sun: "Domingo",
};

export type CompanyAnswers = {
  name?: string;
  website?: string;
  description?: string;
  about?: string;
  address?: string;
  email?: string;
  phone?: string;
  country?: string;
  timezone?: string;
  tone?: string;
  vertical?: string;
  hours?: Partial<Record<Day, DayHours>>;
};

export type Faq = { question: string; answer: string };
export type Product = { name: string; description?: string | null; price?: number | string | null; currency?: string | null; url?: string | null; image_url?: string | null };

/** Estructura de onboarding_runs.answers que guarda el panel. */
export type Answers = {
  company?: CompanyAnswers;
  import?: { url?: string; faqs?: Faq[]; products?: Product[]; pages_read?: number };
  profile?: { about?: string; description?: string; address?: string; email?: string; websites?: string[]; vertical?: string };
  ai?: {
    apply_prompt?: boolean;
    apply_knowledge?: boolean;
    apply_catalog?: boolean;
    apply_typifications?: boolean;
    apply_groups?: boolean;
    apply_hours_message?: boolean;
    hours_message?: string;
    goals?: string[];
  };
  whatsapp?: { coexistence?: boolean };
  test?: { phone?: string };
  [key: string]: unknown;
};

export type WebsiteImport = {
  profile: Partial<CompanyAnswers> & { hours?: unknown };
  faqs: Faq[];
  products: Product[];
  pages_read: number;
};

export type PackTemplate = {
  template_key: string;
  name: string;
  category: string;
  language: string;
  body: string;
  example_values: string[] | Record<string, string>;
  buttons: { type: string; text: string; url?: string }[];
  header?: string | null;
  footer?: string | null;
  recommended: boolean;
};
export type TemplatePack = { pack_key: string; templates: PackTemplate[] };
export type TemplateSubmitResult = {
  submitted: { template_key: string; name: string; status: string; meta_template_id: string | null }[];
  errors: { template_key: string; message: string }[];
};

export type Invitation = {
  id: number;
  email: string;
  name?: string | null;
  role: "admin" | "supervisor" | "agent";
  link?: string | null;
  email_sent?: boolean;
  expires_at: string;
  accepted_at?: string | null;
};

export const INDUSTRIES: { key: string; label: string; icon: string; hint: string }[] = [
  { key: "automotriz", label: "Automotriz", icon: "🚗", hint: "Concesionarios, taller, repuestos" },
  { key: "salud", label: "Salud", icon: "🩺", hint: "Clínicas, consultorios, laboratorios" },
  { key: "educacion", label: "Educación", icon: "🎓", hint: "Colegios, universidades, cursos" },
  { key: "retail", label: "Retail / e-commerce", icon: "🛍️", hint: "Tiendas físicas y en línea" },
  { key: "servicios", label: "Servicios", icon: "🧰", hint: "Profesionales, agencias, soporte" },
  { key: "inmobiliaria", label: "Inmobiliaria", icon: "🏠", hint: "Venta y arriendo de inmuebles" },
  { key: "otro", label: "Otro", icon: "✨", hint: "Lo ajustamos con tus respuestas" },
];

export const TONES = [
  { key: "cercano", label: "Cercano y amable" },
  { key: "profesional", label: "Profesional y formal" },
  { key: "entusiasta", label: "Entusiasta y comercial" },
  { key: "tecnico", label: "Preciso y técnico" },
];

export const COUNTRIES: { code: string; label: string; tz: string }[] = [
  { code: "CO", label: "Colombia", tz: "America/Bogota" },
  { code: "MX", label: "México", tz: "America/Mexico_City" },
  { code: "PE", label: "Perú", tz: "America/Lima" },
  { code: "CL", label: "Chile", tz: "America/Santiago" },
  { code: "AR", label: "Argentina", tz: "America/Argentina/Buenos_Aires" },
  { code: "EC", label: "Ecuador", tz: "America/Guayaquil" },
  { code: "PA", label: "Panamá", tz: "America/Panama" },
  { code: "CR", label: "Costa Rica", tz: "America/Costa_Rica" },
  { code: "GT", label: "Guatemala", tz: "America/Guatemala" },
  { code: "DO", label: "República Dominicana", tz: "America/Santo_Domingo" },
  { code: "UY", label: "Uruguay", tz: "America/Montevideo" },
  { code: "BO", label: "Bolivia", tz: "America/La_Paz" },
  { code: "PY", label: "Paraguay", tz: "America/Asuncion" },
  { code: "VE", label: "Venezuela", tz: "America/Caracas" },
  { code: "ES", label: "España", tz: "Europe/Madrid" },
  { code: "US", label: "Estados Unidos", tz: "America/New_York" },
];

/** Explicación en lenguaje claro de cada validación del número. */
export const CHECK_HELP: Record<string, { label: string; help: string }> = {
  token_valid: { label: "Token de acceso", help: "La credencial con la que enviamos mensajes en nombre de tu número es válida y tiene los permisos necesarios." },
  registered: { label: "Número registrado en la API", help: "El número está activo en la API de WhatsApp Cloud y puede enviar y recibir mensajes." },
  pin: { label: "Verificación en dos pasos", help: "Un PIN de 6 dígitos protege el número contra registros no autorizados. Guárdalo en un lugar seguro." },
  webhook: { label: "Recepción de mensajes (webhook)", help: "Meta nos avisa cada vez que un cliente te escribe. Sin esto los mensajes no llegan a la bandeja." },
  name_approved: { label: "Nombre visible aprobado", help: "Meta revisa el nombre que ven tus clientes. Mientras esté «en revisión» puedes operar, pero con límites más bajos." },
  quality: { label: "Calidad del número", help: "Verde = sin problemas. Amarillo o rojo = muchos clientes bloquean o reportan tus mensajes; baja el volumen de campañas." },
  limit_tier: { label: "Límite de conversaciones", help: "Cuántos clientes distintos puedes contactar primero (con plantilla) en 24 h: 250, 1.000, 10.000, 100.000 o ilimitado. Sube solo con buena calidad." },
  profile: { label: "Perfil de empresa", help: "Descripción, dirección, correo y sitio web que tus clientes ven en tu perfil de WhatsApp." },
  templates_ready: { label: "Plantillas aprobadas", help: "Necesitas al menos una plantilla aprobada para escribirle primero a un cliente o pasadas 24 h de su último mensaje." },
  outbound_test: { label: "Envío de prueba", help: "Enviamos un mensaje real a tu teléfono para confirmar que la salida funciona." },
  inbound_roundtrip: { label: "Respuesta recibida", help: "Respondiste el mensaje de prueba y llegó a la bandeja: la entrada funciona de punta a punta." },
};

export const ROLE_LABEL: Record<Invitation["role"], string> = {
  admin: "Administrador",
  supervisor: "Supervisor",
  agent: "Asesor",
};

export const CATEGORY_LABEL: Record<string, string> = {
  MARKETING: "Marketing",
  UTILITY: "Utilidad",
  AUTHENTICATION: "Autenticación",
};

export const TEMPLATE_STATUS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "info" | "neutral" }> = {
  APPROVED: { label: "Aprobada", tone: "ok" },
  PENDING: { label: "En revisión", tone: "info" },
  IN_APPEAL: { label: "En apelación", tone: "info" },
  REJECTED: { label: "Rechazada", tone: "bad" },
  PAUSED: { label: "Pausada", tone: "warn" },
  DISABLED: { label: "Deshabilitada", tone: "bad" },
  SUBMITTED: { label: "Enviada", tone: "info" },
};
