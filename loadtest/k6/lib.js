// Utilidades compartidas por los escenarios k6.
import crypto from "k6/crypto";

export const BASE = __ENV.BASE_URL || "http://localhost:8000";
export const APP_SECRET = __ENV.WA_APP_SECRET || "";
export const PNID = __ENV.PHONE_NUMBER_ID || "PNID";
export const WABA = __ENV.WABA_ID || "WABA";
export const PHONES = parseInt(__ENV.PHONES || "2000", 10); // clientes simulados distintos

// Teléfono estable por cliente simulado (57 3xx …): reparte la carga entre conversaciones
export function phoneFor(i) {
  return `5730${String(10000000 + (i % PHONES)).padStart(8, "0")}`;
}

// BSUID con el formato de Meta (país + punto + id) para clientes que solo llegan con usuario
export function bsuidFor(i) {
  return `CO.${String(1000000000000 + (i % PHONES))}`;
}

export function uid() {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

// Firma de Meta: X-Hub-Signature-256 = sha256=HMAC(app_secret, cuerpo crudo)
export function signedHeaders(body) {
  const headers = { "Content-Type": "application/json" };
  if (APP_SECRET) headers["X-Hub-Signature-256"] = `sha256=${crypto.hmac("sha256", APP_SECRET, body, "hex")}`;
  return headers;
}

export function envelope(change) {
  return JSON.stringify({ object: "whatsapp_business_account", entry: [{ id: WABA, changes: [change] }] });
}
