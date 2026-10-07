"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { InputType } from "@/lib/flow-types";

export type RefOption = { value: string | number; label: string };

type Loader = () => Promise<RefOption[]>;

const LOADERS: Partial<Record<InputType, Loader>> = {
  group: async () => (await api<{ id: number; name: string }[]>("/api/groups")).map((g) => ({ value: g.id, label: g.name })),
  agent: async () => (await api<{ id: number; name: string }[]>("/api/agents")).map((a) => ({ value: a.id, label: a.name })),
  field: async () =>
    (await api<{ key: string; label: string }[]>("/api/contact-fields")).map((f) => ({ value: f.key, label: f.label })),
  typification: async () =>
    ((await api<{ typifications: string[] }>("/api/settings/conversations")).typifications ?? []).map((t) => ({
      value: t,
      label: t,
    })),
  template: async () =>
    (await api<{ name: string; language: string; status: string }[]>("/api/templates"))
      .filter((t) => t.status === "APPROVED")
      .map((t) => ({ value: t.name, label: `${t.name} (${t.language})` })),
  resource: async () => (await api<{ id: number; name: string }[]>("/api/resources")).map((r) => ({ value: r.id, label: r.name })),
  ai_agent: async () => (await api<{ id: number; name: string }[]>("/api/bots")).map((b) => ({ value: b.id, label: b.name })),
  tag: async () => (await api<{ name: string }[]>("/api/conversation-tags")).map((t) => ({ value: t.name, label: t.name })),
};

const cache = new Map<InputType, Promise<RefOption[]>>();

export function hasRefOptions(type: InputType): boolean {
  return type in LOADERS;
}

/** Opciones de referencia (grupos, asesores, campos...) para los selectores de los bloques. */
export function useRefOptions(type: InputType): RefOption[] | null {
  const [options, setOptions] = useState<RefOption[] | null>(null);
  useEffect(() => {
    const loader = LOADERS[type];
    if (!loader) return;
    let p = cache.get(type);
    if (!p) {
      p = loader().catch(() => []);
      cache.set(type, p);
    }
    let alive = true;
    p.then((o) => alive && setOptions(o));
    return () => {
      alive = false;
    };
  }, [type]);
  return options;
}
