"use client";

import { useParams } from "next/navigation";
import JourneyEditor from "@/components/journeys/JourneyEditor";

export default function JourneyPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  if (!Number.isFinite(id)) return <div className="error-box">Journey inválido</div>;
  return <JourneyEditor journeyId={id} />;
}
