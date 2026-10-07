// Productividad del asesor (§18.3): notificaciones, respuestas rápidas v2, línea de tiempo, iniciar conversación.

export type NotificationType =
  | "assigned"
  | "message"
  | "mention"
  | "followup_due"
  | "callback_due"
  | "sla_breach"
  | "transfer"
  | "call"
  | "system"
  | "qa_review"
  | "stage";

export type AppNotification = {
  id: number;
  type: NotificationType;
  title: string;
  body: string | null;
  link: string | null;
  data: Record<string, unknown>;
  read: boolean;
  read_at: string | null;
  created_at: string;
};

export type NotificationPage = { items: AppNotification[]; next_cursor: number | null; unread: number };

export type NotificationPrefs = { sound: boolean; desktop: boolean; types: Record<NotificationType, boolean> };

export const NOTIFICATION_LABELS: Record<NotificationType, string> = {
  assigned: "Conversación asignada",
  message: "Mensaje en mis conversaciones",
  mention: "Menciones en notas",
  followup_due: "Seguimientos vencidos",
  callback_due: "Llamadas pendientes",
  sla_breach: "Alertas de SLA",
  transfer: "Transferencias",
  call: "Llamadas entrantes",
  system: "Sistema",
  qa_review: "Revisiones de calidad",
  stage: "Cambios de etapa",
};

export const NOTIFICATION_ICONS: Record<NotificationType, string> = {
  assigned: "👤",
  message: "💬",
  mention: "@",
  followup_due: "📌",
  callback_due: "📞",
  sla_breach: "⏱️",
  transfer: "🔁",
  call: "📞",
  system: "ℹ️",
  qa_review: "⭐",
  stage: "📈",
};

export type QuickReplyV2 = {
  id: number;
  shortcut: string;
  title: string | null;
  category: string | null;
  text: string;
  rendered: string;
  resource_ids: number[];
  attachments: { id: number; name: string; mime: string }[];
  group_ids: number[];
  is_active: boolean;
  usage_count: number;
  updated_at: string;
};

export type TimelineItem = {
  at: string;
  type: string;
  label: string;
  icon: string;
  detail: string | null;
  actor_type: string;
  actor: string | null;
};

export const QUICK_VARIABLES = [
  { key: "{{client_name}}", label: "Nombre del cliente" },
  { key: "{{client_first_name}}", label: "Primer nombre del cliente" },
  { key: "{{agent_name}}", label: "Tu nombre" },
  { key: "{{group_name}}", label: "Grupo" },
  { key: "{{company_name}}", label: "Empresa" },
];
