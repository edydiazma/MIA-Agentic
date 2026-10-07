// Tormenta de webhooks de WhatsApp: texto, medios, estados, clientes solo con BSUID y referrals Click to WhatsApp.
//
//   k6 run -e BASE_URL=https://staging.tu-dominio.com -e WA_APP_SECRET=... -e PHONE_NUMBER_ID=... \
//          -e WABA_ID=... -e PEAK_RPS=200 -e PHONES=5000 loadtest/k6/webhook_storm.js
//
// Rampa: 10 → PEAK_RPS mensajes/s en 5 min, sostiene 10 min y baja. El backend debe responder 200 rápido (los
// webhooks se guardan en inbound_events y se procesan en segundo plano): el umbral principal es la latencia del 200.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";
import { BASE, PNID, bsuidFor, envelope, phoneFor, signedHeaders, uid } from "./lib.js";

const PEAK = parseInt(__ENV.PEAK_RPS || "100", 10);
const sent = new Counter("webhooks_sent");

export const options = {
  scenarios: {
    storm: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: Math.max(50, PEAK),
      maxVUs: PEAK * 4,
      stages: [
        { target: PEAK, duration: __ENV.RAMP || "5m" },
        { target: PEAK, duration: __ENV.HOLD || "10m" },
        { target: 0, duration: "1m" },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.005"], // < 0,5 % de errores
    http_req_duration: ["p(95)<300", "p(99)<800"], // el 200 a Meta debe ser rápido (Meta reintenta si tarda)
  },
};

function contactBlock(i, withPhone) {
  const c = { profile: { name: `Cliente ${i}` }, user_id: bsuidFor(i) };
  if (withPhone) c.wa_id = phoneFor(i);
  return c;
}

function message(i) {
  const r = Math.random();
  const withPhone = r > 0.05; // 5 % de clientes llegan solo con usuario (BSUID)
  const base = { id: `wamid.lt.${uid()}`, timestamp: `${Math.floor(Date.now() / 1000)}`, from_user_id: bsuidFor(i) };
  if (withPhone) base.from = phoneFor(i);
  let msg;
  if (r < 0.10) {
    // Click to WhatsApp (anuncio de Meta)
    msg = { ...base, type: "text", text: { body: "Hola, quiero la promo de lanzamiento" },
      referral: { source_type: "ad", source_id: `1202${i % 50}`, source_url: "https://fb.me/xyz",
        headline: "Promo de lanzamiento", ctwa_clid: `CLID${uid()}` } };
  } else if (r < 0.18) {
    msg = { ...base, type: "image", image: { id: `media${uid()}`, mime_type: "image/jpeg", caption: "Foto" } };
  } else {
    msg = { ...base, type: "text", text: { body: ["hola", "precio?", "quiero cotizar", "gracias", "a qué hora abren"][i % 5] } };
  }
  return {
    field: "messages",
    value: { messaging_product: "whatsapp", metadata: { phone_number_id: PNID },
      contacts: [contactBlock(i, withPhone)], messages: [msg] },
  };
}

function status(i) {
  return {
    field: "messages",
    value: { messaging_product: "whatsapp", metadata: { phone_number_id: PNID },
      statuses: [{ id: `wamid.out.${uid()}`, status: ["sent", "delivered", "read"][i % 3],
        timestamp: `${Math.floor(Date.now() / 1000)}`, recipient_id: phoneFor(i), recipient_user_id: bsuidFor(i) }] },
  };
}

export default function () {
  const i = Math.floor(Math.random() * 1e9);
  const body = envelope(Math.random() < 0.3 ? status(i) : message(i)); // 30 % estados, 70 % mensajes
  const res = http.post(`${BASE}/webhooks/whatsapp`, body, { headers: signedHeaders(body), tags: { kind: "webhook" } });
  sent.add(1);
  check(res, { "200": (r) => r.status === 200 });
}
