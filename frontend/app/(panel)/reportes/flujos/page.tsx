"use client";

import { ComingSoon, PageHeader } from "@/components/ui";

export default function Page() {
  return (
    <>
      <PageHeader title="Análisis de flujos" />
      <ComingSoon
        title="Análisis de flujos"
        description="Embudo paso a paso de cada flujo conversacional: cuántos clientes entran a cada nodo, dónde abandonan y qué opciones eligen."
        needs={[
          "El constructor visual de flujos (Automatizaciones → Gestión de flujos), que es una fase posterior",
          "Registrar en el backend cada paso que recorre un cliente dentro de un flujo",
        ]}
      />
    </>
  );
}
