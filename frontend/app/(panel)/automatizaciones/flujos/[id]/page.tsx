"use client";

import { useParams } from "next/navigation";
import FlowEditor from "@/components/flows/FlowEditor";

export default function FlowEditorPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  if (!Number.isFinite(id)) return <div className="error-box">Flujo inválido</div>;
  return <FlowEditor flowId={id} />;
}
