/** Llaves de la API pública (/v1). La llave completa solo llega al crear o rotar. */
export type ApiKeyOut = {
  id: number;
  name: string;
  prefix: string;
  scopes: string[];
  rate_limit_per_min: number;
  last_used_at: string | null;
  last_used_ip: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
  webhooks: number;
  requests_30d: number;
  key: string | null;
};

export type ApiKeyIn = {
  name: string;
  scopes: string[];
  rate_limit_per_min: number;
  expires_at: string | null;
};

export type ScopeOption = { key: string; label: string };
