// Endpoints públicos: tracking web (/t/collect), chat web (/w/*), webhooks entrantes (/hooks/*) y API (/v1/*).
// Respetan los límites de uso: se esperan algunos 429 cuando una misma IP/llave supera su cupo — eso NO es error.
//
//   k6 run -e BASE_URL=https://staging... -e SITE_KEY=<tracking public_key> -e SITE_ORIGIN=https://sitio-permitido.com \
//          -e WIDGET_KEY=<chat web key> \
//          -e HOOK_SLUG=<slug> -e HOOK_TOKEN=whk_... -e API_KEY=wak_live_... loadtest/k6/public_endpoints.js
// Cualquier escenario cuya variable falte se omite.
import http from "k6/http";
import { check } from "k6";
import { Rate } from "k6/metrics";
import { BASE, phoneFor, uid } from "./lib.js";

const throttled = new Rate("throttled_429");
const RPS = parseInt(__ENV.RPS || "20", 10);
const DURATION = __ENV.DURATION || "5m";

function scenario(fn) {
  return { executor: "constant-arrival-rate", rate: RPS, timeUnit: "1s", duration: DURATION,
    preAllocatedVUs: RPS * 2, maxVUs: RPS * 6, exec: fn };
}

const scenarios = {};
if (__ENV.SITE_KEY) scenarios.tracking = scenario("tracking");
if (__ENV.WIDGET_KEY) scenarios.webchat = scenario("webchat");
if (__ENV.HOOK_SLUG && __ENV.HOOK_TOKEN) scenarios.hooks = scenario("hooks");
if (__ENV.API_KEY) scenarios.api = scenario("api");

export const options = {
  scenarios,
  thresholds: {
    // 429 no cuenta como fallo: el límite de uso está haciendo su trabajo
    checks: ["rate>0.99"],
    "http_req_duration{kind:public}": ["p(95)<400"],
  },
};

const ok = (r) => r.status < 400 || r.status === 429;
const JSON_HEADERS = { "Content-Type": "application/json" };

export function tracking() {
  // SITE_ORIGIN debe estar en los dominios permitidos del sitio (si no → 403)
  const origin = __ENV.SITE_ORIGIN || "https://sitio.test";
  const r = http.post(`${BASE}/t/collect`, JSON.stringify({ k: __ENV.SITE_KEY, vid: uid(), url: `${origin}/?utm_source=lt`,
    params: { utm_source: "loadtest", gclid: `G${uid()}` } }),
    { headers: { "Content-Type": "text/plain", Origin: origin }, tags: { kind: "public" } });
  throttled.add(r.status === 429);
  check(r, { tracking: ok });
}

export function webchat() {
  const s = http.post(`${BASE}/w/session`, JSON.stringify({ key: __ENV.WIDGET_KEY, visitor_id: `lt-${uid()}` }),
    { headers: JSON_HEADERS, tags: { kind: "public" } });
  throttled.add(s.status === 429);
  check(s, { session: ok });
  if (s.status !== 200) return;
  const m = http.post(`${BASE}/w/messages`, JSON.stringify({ key: __ENV.WIDGET_KEY, token: s.json("token"), text: "hola" }),
    { headers: JSON_HEADERS, tags: { kind: "public" } });
  throttled.add(m.status === 429);
  check(m, { message: ok });
}

export function hooks() {
  const r = http.post(`${BASE}/hooks/${__ENV.HOOK_SLUG}`, JSON.stringify({ telefono: phoneFor(Math.floor(Math.random() * 1e6)),
    nombre: "Cliente Carga" }), { headers: { ...JSON_HEADERS, "X-Hook-Token": __ENV.HOOK_TOKEN, "Idempotency-Key": uid() },
    tags: { kind: "public" } });
  throttled.add(r.status === 429);
  check(r, { hook: ok });
}

export function api() {
  const r = http.get(`${BASE}/v1/contacts?limit=20`, { headers: { Authorization: `Bearer ${__ENV.API_KEY}` }, tags: { kind: "public" } });
  throttled.add(r.status === 429);
  check(r, { api: ok });
}
