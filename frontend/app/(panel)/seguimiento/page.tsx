"use client";

import { useState } from "react";
import { PageHeader, Tabs } from "@/components/ui";
import FollowUps from "@/components/clients/FollowUps";
import Appointments from "@/components/clients/Appointments";

export default function SeguimientoPage() {
  const [tab, setTab] = useState<"followups" | "appointments">("followups");
  return (
    <>
      <PageHeader title="Seguimiento" subtitle="Tareas pendientes con tus clientes y agenda de citas" />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          ["followups", "Seguimientos"],
          ["appointments", "Citas"],
        ]}
      />
      {tab === "followups" ? <FollowUps /> : <Appointments />}
    </>
  );
}
