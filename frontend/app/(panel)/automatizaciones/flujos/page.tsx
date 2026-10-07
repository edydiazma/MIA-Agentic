import { PageHeader } from "@/components/ui";
import FlowList from "@/components/flows/FlowList";

export default function FlujosPage() {
  return (
    <>
      <PageHeader
        title="Gestión de flujos"
        subtitle="Conversaciones guiadas con bloques: modo Junior (íconos grandes) o Avanzado (estilo Scratch 3)."
      />
      <FlowList />
    </>
  );
}
