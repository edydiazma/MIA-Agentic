// Tipos del módulo de voz (WhatsApp Business Calling API + agentes de voz con IA).

export type CallStatus =
  | "ringing"
  | "pre_accepted"
  | "connected"
  | "transferring"
  | "ended"
  | "missed"
  | "rejected"
  | "failed";
export type CallHandler = "voice_agent" | "agent";

export type Call = {
  id: number;
  organization_id: number;
  channel_id: number;
  contact_id: number;
  contact_name: string | null;
  wa_id: string | null;
  conversation_id: number | null;
  wa_call_id: string;
  direction: string;
  status: CallStatus;
  handled_by: CallHandler | null;
  voice_agent_id: number | null;
  agent_id: number | null;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  duration_s: number | null;
  end_reason: string | null;
  has_recording: boolean;
  summary: string | null;
};

/** Evento "call.incoming": suena en el navegador. `answer` = contestar la oferta de Meta; `bridge` = transferencia del agente de voz. */
export type IncomingCall = Call & { sdp_offer?: string | null; targets?: number[]; mode: "answer" | "bridge" };

export type CallTurn = { speaker: "contact" | "voice_agent" | "agent"; text: string; start_ms: number | null; created_at: string };
export type CallEventRow = { type: string; created_at: string; payload: Record<string, unknown> };
export type CallDetail = Call & { transcript: string | null; turns: CallTurn[]; events: CallEventRow[] };

export type CallStats = {
  range: { start: string; end: string };
  total: number;
  answered: number;
  answer_rate: number | null;
  avg_duration_s: number | null;
  talk_minutes: number;
  by_status: Partial<Record<CallStatus, number>>;
  by_handler: Partial<Record<CallHandler, number>>;
  daily: { day: string; calls: number; answered: number }[];
  minutes_month: { metric: string; used: number; limit: number | null; [k: string]: unknown };
};

export type VoiceAgent = {
  id: number;
  name: string;
  enabled: boolean;
  ai_agent_id: number | null;
  provider: "openai_realtime" | "pipeline";
  model: string;
  voice: string;
  language: string;
  greeting: string;
  max_duration_s: number;
  transfer_group_id: number | null;
  record_calls: boolean;
  settings: Record<string, unknown>;
};
export type VoiceAgentInput = Omit<VoiceAgent, "id">;

export type CallingHours = { days: number[]; start: string; end: string };
export type CallingChannel = {
  id: number;
  name: string | null;
  display_phone: string | null;
  phone_number_id: string;
  calling_enabled: boolean;
  calling_hours: CallingHours | null;
  voice_agent_id: number | null;
};

export const CALL_STATUS_LABEL: Record<CallStatus, string> = {
  ringing: "Sonando",
  pre_accepted: "Conectando",
  connected: "En curso",
  transferring: "Transfiriendo",
  ended: "Finalizada",
  missed: "Perdida",
  rejected: "Rechazada",
  failed: "Fallida",
};
export const CALL_STATUS_TONE: Record<CallStatus, "neutral" | "ok" | "warn" | "bad" | "info"> = {
  ringing: "info",
  pre_accepted: "info",
  connected: "ok",
  transferring: "warn",
  ended: "neutral",
  missed: "bad",
  rejected: "warn",
  failed: "bad",
};
export const HANDLER_LABEL: Record<CallHandler, string> = { voice_agent: "Agente de voz", agent: "Asesor" };
export const SPEAKER_LABEL: Record<CallTurn["speaker"], string> = { contact: "Cliente", voice_agent: "Agente de voz", agent: "Asesor" };
export const DAY_LABEL = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
export const VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"];

export const fmtDuration = (s: number | null | undefined) => {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};
