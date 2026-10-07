"use client";

import { ComingSoon, PageHeader } from "@/components/ui";

export default function Page() {
  return (
    <>
      <PageHeader title="Tráfico web" />
      <ComingSoon
        title="Tráfico web"
        description="Qué páginas y fuentes del sitio generan conversaciones de WhatsApp."
        needs={[
          "Un script en el sitio (vía Google Tag Manager) que registre la sesión y la fuente antes del clic al botón de WhatsApp",
          "Pasar un identificador en el enlace wa.me para unir la visita con la conversación",
        ]}
      />
    </>
  );
}
