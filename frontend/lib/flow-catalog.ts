// Catálogo de bloques (respaldo local de GET /api/flows/catalog). Ver docs/flows.md §3.
// Colores por categoría al estilo Scratch 3.
import type { CatalogBlock, CatalogInput, FlowCatalog, InputType } from "./flow-types";

const i = (name: string, label: string, type: InputType, required = false, options?: CatalogInput["options"]): CatalogInput => ({
  name,
  label,
  type,
  required,
  ...(options ? { options } : {}),
});
const opts = (...pairs: [string, string][]) => pairs.map(([value, label]) => ({ value, label }));

const MATCH = opts(["contains", "Contiene"], ["exact", "Exacto"]);
const STAGES = opts(["lead", "Lead"], ["prospect", "Prospecto"], ["client", "Cliente"], ["lost", "Perdido"]);

const blocks: CatalogBlock[] = [
  // ---- Eventos (sombrero) ----
  { type: "inbound_message", category: "events", label: "Cuando llega un mensaje", icon: "💬", junior: true, shape: "hat",
    inputs: [i("keywords", "Solo si contiene (opcional)", "options")] },
  { type: "keyword", category: "events", label: "Cuando el cliente dice…", icon: "🔑", junior: true, shape: "hat",
    inputs: [i("keywords", "Palabras clave", "options", true), i("match", "Coincidencia", "select", true, MATCH)] },
  { type: "handoff", category: "events", label: "Cuando se transfiere a asesor", icon: "🙋", junior: true, shape: "hat", inputs: [] },
  { type: "close", category: "events", label: "Cuando se cierra la conversación", icon: "🔒", junior: true, shape: "hat", inputs: [] },
  { type: "schedule", category: "events", label: "Cada cierto tiempo", icon: "⏰", junior: true, shape: "hat",
    inputs: [i("every_minutes", "Cada (minutos)", "duration", true), i("at", "A las (HH:MM, opcional)", "text")] },
  { type: "webhook", category: "events", label: "Cuando llega un webhook", icon: "🪝", junior: false, shape: "hat", inputs: [] },
  { type: "manual", category: "events", label: "Al iniciarlo manualmente", icon: "▶️", junior: true, shape: "hat", inputs: [] },
  { type: "campaign_reply", category: "events", label: "Cuando responden una campaña", icon: "📣", junior: true, shape: "hat",
    inputs: [i("campaign", "Campaña (opcional)", "text")] },

  // ---- Mensajes ----
  { type: "send_text", category: "messages", label: "Enviar texto", icon: "✉️", junior: true, shape: "stack",
    inputs: [i("text", "Texto", "text", true)] },
  { type: "send_media", category: "messages", label: "Enviar archivo", icon: "🖼️", junior: true, shape: "stack",
    inputs: [i("resource_id", "Recurso", "resource"), i("url", "o URL", "text"), i("caption", "Texto", "text")] },
  { type: "send_buttons", category: "messages", label: "Enviar botones", icon: "🔘", junior: true, shape: "stack",
    inputs: [i("text", "Texto", "text", true), i("buttons", "Botones (máx. 3)", "buttons", true)] },
  { type: "send_list", category: "messages", label: "Enviar lista", icon: "📋", junior: false, shape: "stack",
    inputs: [i("text", "Texto", "text", true), i("button", "Texto del botón", "text", true), i("items", "Opciones", "options", true)] },
  { type: "send_template", category: "messages", label: "Enviar plantilla", icon: "🧾", junior: true, shape: "stack",
    inputs: [i("name", "Plantilla", "template", true), i("values", "Variables", "options")] },
  { type: "send_product", category: "messages", label: "Enviar producto", icon: "🛍️", junior: true, shape: "stack",
    inputs: [i("sku", "SKU", "text"), i("search", "o buscar", "text")] },

  // ---- Esperas ----
  { type: "wait_reply", category: "wait", label: "Esperar respuesta", icon: "⏳", junior: true, shape: "stack",
    inputs: [i("timeout_min", "Máximo (minutos)", "duration", true), i("save_to", "Guardar en variable", "text")] },
  { type: "wait_time", category: "wait", label: "Esperar", icon: "🕒", junior: true, shape: "stack",
    inputs: [i("minutes", "Minutos", "duration", true)] },

  // ---- Control ----
  { type: "if", category: "control", label: "Si", icon: "❓", junior: true, shape: "c",
    inputs: [i("condition", "Condición", "condition", true)], branches: ["then", "else"] },
  { type: "switch_reply", category: "control", label: "Si responde…", icon: "🔀", junior: true, shape: "c",
    inputs: [i("options", "Opciones", "options", true), i("timeout_min", "Esperar (minutos)", "duration")], branches: ["options"] },
  { type: "repeat", category: "control", label: "Repetir", icon: "🔁", junior: false, shape: "c",
    inputs: [i("times", "Veces (máx. 20)", "number", true)], branches: ["body"] },
  { type: "stop", category: "control", label: "Detener", icon: "⛔", junior: true, shape: "cap", inputs: [] },
  { type: "go_to_script", category: "control", label: "Ir al guion", icon: "↪️", junior: false, shape: "cap",
    inputs: [i("script_id", "Guion", "text", true)] },

  // ---- IA (Cortex) ----
  { type: "ai_reply", category: "ai", label: "Responder con IA", icon: "🤖", junior: true, shape: "stack",
    inputs: [i("ai_agent_id", "Agente de IA", "ai_agent"), i("instruction", "Instrucción", "text")] },
  { type: "ai_extract", category: "ai", label: "Extraer datos con IA", icon: "🧲", junior: false, shape: "stack",
    inputs: [i("fields", "Campos (nombres)", "options", true), i("save_to", "Guardar en variable", "text", true)] },
  { type: "ai_classify", category: "ai", label: "Clasificar con IA", icon: "🏷️", junior: false, shape: "stack",
    inputs: [i("options", "Opciones", "options", true), i("save_to", "Guardar en variable", "text", true)] },
  { type: "ai_decide", category: "ai", label: "Decidir con IA", icon: "🧠", junior: false, shape: "c",
    inputs: [i("question", "Pregunta", "text", true), i("options", "Opciones", "options", true)], branches: ["options"] },

  // ---- Conversación ----
  { type: "handoff_to_agent", category: "conversation", label: "Pasar a asesor", icon: "🙋", junior: true, shape: "stack",
    inputs: [i("group_id", "Grupo", "group"), i("reason", "Motivo", "text")] },
  { type: "assign", category: "conversation", label: "Asignar", icon: "👤", junior: false, shape: "stack",
    inputs: [i("agent_id", "Asesor", "agent"), i("group_id", "Grupo", "group")] },
  { type: "tag", category: "conversation", label: "Etiquetar", icon: "🏷", junior: true, shape: "stack",
    inputs: [i("tag", "Etiqueta", "tag", true)] },
  { type: "untag", category: "conversation", label: "Quitar etiqueta", icon: "✂️", junior: false, shape: "stack",
    inputs: [i("tag", "Etiqueta", "tag", true)] },
  { type: "typify_close", category: "conversation", label: "Cerrar con tipificación", icon: "✅", junior: true, shape: "cap",
    inputs: [i("typification", "Tipificación", "typification", true)] },

  // ---- CRM ----
  { type: "set_field", category: "crm", label: "Guardar dato del cliente", icon: "📝", junior: true, shape: "stack",
    inputs: [i("field", "Campo", "field", true), i("value", "Valor", "text", true)] },
  { type: "set_stage", category: "crm", label: "Cambiar etapa", icon: "📈", junior: true, shape: "stack",
    inputs: [i("stage", "Etapa", "select", true, STAGES)] },
  { type: "update_memory", category: "crm", label: "Actualizar memoria", icon: "🧠", junior: false, shape: "stack",
    inputs: [i("text", "Texto (vacío = resumir con IA)", "text")] },
  { type: "create_followup", category: "crm", label: "Crear seguimiento", icon: "📌", junior: true, shape: "stack",
    inputs: [i("agent_id", "Asesor", "agent"), i("in_hours", "En (horas)", "number", true), i("note", "Nota", "text", true)] },
  { type: "book_appointment", category: "crm", label: "Agendar cita", icon: "📅", junior: false, shape: "stack",
    inputs: [i("date", "Fecha (AAAA-MM-DD)", "text", true), i("time", "Hora (HH:MM)", "text", true)] },

  // ---- Datos ----
  { type: "set_var", category: "data", label: "Fijar variable", icon: "📦", junior: false, shape: "stack",
    inputs: [i("name", "Variable", "text", true), i("value", "Valor", "text")] },
  { type: "change_var", category: "data", label: "Sumar a variable", icon: "➕", junior: false, shape: "stack",
    inputs: [i("name", "Variable", "text", true), i("delta", "Cantidad", "number", true)] },
  { type: "http_request", category: "data", label: "Llamar a una API", icon: "🌐", junior: false, shape: "stack",
    inputs: [
      i("method", "Método", "select", true, opts(["GET", "GET"], ["POST", "POST"], ["PUT", "PUT"], ["PATCH", "PATCH"], ["DELETE", "DELETE"])),
      i("url", "URL (https)", "text", true),
      i("body", "Cuerpo (JSON)", "text"),
      i("save_to", "Guardar en variable", "text"),
    ] },

  // ---- Operadores (reporteros: solo dentro de condiciones) ----
  { type: "eq", category: "operators", label: "=", icon: "=", junior: false, shape: "reporter",
    inputs: [i("left", "Izquierda", "text"), i("right", "Derecha", "text")] },
  { type: "gt", category: "operators", label: ">", icon: ">", junior: false, shape: "reporter",
    inputs: [i("left", "Izquierda", "text"), i("right", "Derecha", "text")] },
  { type: "lt", category: "operators", label: "<", icon: "<", junior: false, shape: "reporter",
    inputs: [i("left", "Izquierda", "text"), i("right", "Derecha", "text")] },
  { type: "contains", category: "operators", label: "contiene", icon: "∋", junior: false, shape: "reporter",
    inputs: [i("left", "Texto", "text"), i("right", "Busca", "text")] },
  { type: "and", category: "operators", label: "y", icon: "∧", junior: false, shape: "reporter",
    inputs: [i("left", "A", "condition"), i("right", "B", "condition")] },
  { type: "or", category: "operators", label: "o", icon: "∨", junior: false, shape: "reporter",
    inputs: [i("left", "A", "condition"), i("right", "B", "condition")] },
  { type: "not", category: "operators", label: "no", icon: "¬", junior: false, shape: "reporter",
    inputs: [i("value", "Condición", "condition")] },
  { type: "join", category: "operators", label: "unir", icon: "⧺", junior: false, shape: "reporter",
    inputs: [i("left", "A", "text"), i("right", "B", "text")] },
  { type: "length", category: "operators", label: "longitud de", icon: "#", junior: false, shape: "reporter",
    inputs: [i("value", "Texto", "text")] },
];

export const FALLBACK_CATALOG: FlowCatalog = {
  categories: [
    { key: "events", label: "Eventos", color: "#FFBF00" },
    { key: "messages", label: "Mensajes", color: "#4C97FF" },
    { key: "wait", label: "Esperas", color: "#5CB1D6" },
    { key: "control", label: "Control", color: "#FFAB19" },
    { key: "ai", label: "IA (Cortex)", color: "#9966FF" },
    { key: "conversation", label: "Conversación", color: "#CF63CF" },
    { key: "crm", label: "CRM", color: "#0FBD8C" },
    { key: "data", label: "Datos", color: "#FF8C1A" },
    { key: "operators", label: "Operadores", color: "#59C059" },
  ],
  blocks,
};

export const TRIGGER_TYPES: { value: string; label: string }[] = blocks
  .filter((b) => b.shape === "hat")
  .map((b) => ({ value: b.type, label: b.label }));

export const CONDITION_OPS = ["eq", "gt", "lt", "contains", "and", "or", "not", "join", "length"] as const;
