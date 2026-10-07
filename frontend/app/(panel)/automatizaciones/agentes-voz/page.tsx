import { ComingSoon, PageHeader } from "@/components/ui";

export default function AgentesVozPage() {
  return (
    <>
      <PageHeader title="Agentes de voz" subtitle="Automatizaciones · Llamadas" />
      <ComingSoon
        title="Agentes de voz con IA"
        description="Atender y hacer llamadas con un agente de IA que conversa por voz, con transferencia a un asesor humano."
        needs={[
          "WhatsApp Business Calling API (llamadas por WhatsApp) o una troncal SIP / proveedor de telefonía.",
          "Reconocimiento y síntesis de voz en tiempo real (streaming) conectados al agente.",
          "Grabación, transcripción y resumen de cada llamada.",
        ]}
      />
    </>
  );
}
