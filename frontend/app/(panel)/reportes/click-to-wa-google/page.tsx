"use client";

import { ComingSoon, PageHeader } from "@/components/ui";

export default function Page() {
  return (
    <>
      <PageHeader title="Click to WA Google" />
      <ComingSoon
        title="Click to WA Google"
        description="Conversaciones que llegan desde anuncios de Google Ads, con conversiones de vuelta a Google."
        needs={[
          "Enlaces wa.me con un código de referencia en el mensaje prellenado o en UTM, creados desde GTM en el sitio",
          "Guardar el gclid de la visita y asociarlo a la conversación cuando el cliente escribe",
          "Subir las ventas como conversiones offline a Google Ads",
        ]}
      />
    </>
  );
}
