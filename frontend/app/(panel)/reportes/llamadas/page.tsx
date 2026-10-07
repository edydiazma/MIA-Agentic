"use client";

import { ComingSoon, PageHeader } from "@/components/ui";

export default function Page() {
  return (
    <>
      <PageHeader title="Llamadas de asesores" />
      <ComingSoon
        title="Llamadas de asesores"
        description="Duración, atendidas y perdidas de las llamadas de voz por WhatsApp de cada asesor."
        needs={[
          "Activar la WhatsApp Business Calling API en el número (o integrar una telefonía como Twilio/Talkdesk)",
          "Registrar cada llamada y su resultado en la plataforma",
        ]}
      />
    </>
  );
}
