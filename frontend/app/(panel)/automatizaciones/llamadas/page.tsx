import { ComingSoon, PageHeader } from "@/components/ui";

export default function HistorialLlamadasPage() {
  return (
    <>
      <PageHeader title="Historial de llamadas" subtitle="Automatizaciones · Llamadas" />
      <ComingSoon
        title="Historial de llamadas"
        description="Registro de llamadas de asesores y agentes de voz: duración, resultado, grabación y transcripción."
        needs={[
          "Integración de telefonía (WhatsApp Business Calling API o SIP) que reporte los eventos de cada llamada.",
          "Almacenamiento de grabaciones y su transcripción.",
        ]}
      />
    </>
  );
}
