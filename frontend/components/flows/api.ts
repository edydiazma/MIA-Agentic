"use client";

import { useEffect, useState } from "react";
import { api, ApiError, send } from "@/lib/api";
import { FALLBACK_CATALOG } from "@/lib/flow-catalog";
import type {
  FlowCatalog,
  FlowDefinition,
  FlowDetail,
  FlowError,
  Flow,
  FlowTestResult,
  FlowVersion,
  FlowVersionDetail,
} from "@/lib/flow-types";

/** Catálogo del servidor; si el endpoint no existe aún (404) se usa el respaldo local. */
export function useCatalog(): FlowCatalog {
  const [catalog, setCatalog] = useState<FlowCatalog>(FALLBACK_CATALOG);
  useEffect(() => {
    api<FlowCatalog>("/api/flows/catalog")
      .then((c) => c?.blocks?.length && setCatalog(c))
      .catch(() => {});
  }, []);
  return catalog;
}

export class FlowValidationError extends Error {
  constructor(public errors: FlowError[]) {
    super(errors.map((e) => e.message).join("; "));
  }
}

/** Guarda una versión; si el servidor responde 422 con errores, los devuelve estructurados. */
export async function saveVersion(flowId: number, definition: FlowDefinition, changeNote?: string) {
  try {
    return await send<{ id: number; version: number; errors?: FlowError[] }>(
      `/api/flows/${flowId}/versions`,
      "POST",
      { definition, change_note: changeNote || null },
    );
  } catch (e) {
    if (e instanceof ApiError && e.status === 422) {
      try {
        const parsed = JSON.parse(e.message);
        if (Array.isArray(parsed)) throw new FlowValidationError(parsed as FlowError[]);
        if (Array.isArray(parsed?.errors)) throw new FlowValidationError(parsed.errors as FlowError[]);
      } catch (inner) {
        if (inner instanceof FlowValidationError) throw inner;
      }
      throw new FlowValidationError([{ path: "", message: e.message }]);
    }
    throw e;
  }
}

export const flowsApi = {
  list: () => api<Flow[]>("/api/flows"),
  get: (id: number) => api<FlowDetail>(`/api/flows/${id}`),
  create: (body: Partial<Flow>) => send<Flow>("/api/flows", "POST", body),
  update: (id: number, body: Partial<Flow>) => send<Flow>(`/api/flows/${id}`, "PUT", body),
  versions: (id: number) => api<FlowVersion[]>(`/api/flows/${id}/versions`),
  version: (id: number, versionId: number) => api<FlowVersionDetail>(`/api/flows/${id}/versions/${versionId}`),
  publish: (id: number, versionId: number) => send<Flow>(`/api/flows/${id}/publish`, "POST", { version_id: versionId }),
  pause: (id: number) => send<Flow>(`/api/flows/${id}/pause`, "POST"),
  archive: (id: number) => send<Flow>(`/api/flows/${id}/archive`, "POST"),
  test: (id: number, text: string, definition?: FlowDefinition) =>
    send<FlowTestResult>(`/api/flows/${id}/test`, "POST", { text, definition }),
};
