// Carga del panel: asesores que abren la bandeja, leen conversaciones, responden y consultan reportes.
//
//   k6 run -e BASE_URL=https://staging... -e AGENT_EMAIL=carga@empresa.com -e AGENT_PASSWORD=... \
//          -e AGENTS=50 loadtest/k6/panel_mix.js
//
// Cada VU es un asesor con su sesión. Mezcla aproximada por iteración: lista (100 %), abrir conversación (80 %),
// responder (25 %), reportes (10 %). Responder envía a WhatsApp: usa WA_GRAPH_BASE → loadtest/fake_meta.py.
import http from "k6/http";
import { check, sleep } from "k6";
import { BASE } from "./lib.js";

const AGENTS = parseInt(__ENV.AGENTS || "30", 10);

export const options = {
  scenarios: {
    agents: { executor: "constant-vus", vus: AGENTS, duration: __ENV.DURATION || "10m" },
  },
  thresholds: {
    "http_req_failed{kind:panel}": ["rate<0.01"],
    "http_req_duration{endpoint:inbox}": ["p(95)<500"],
    "http_req_duration{endpoint:messages}": ["p(95)<500"],
    "http_req_duration{endpoint:send}": ["p(95)<1500"], // incluye el envío a la Graph API (falsa)
    "http_req_duration{endpoint:reports}": ["p(95)<2000"],
  },
};

export function setup() {
  const r = http.post(`${BASE}/api/auth/login`, JSON.stringify({
    email: __ENV.AGENT_EMAIL, password: __ENV.AGENT_PASSWORD }), { headers: { "Content-Type": "application/json" } });
  check(r, { login: (x) => x.status === 200 && !!x.json("access_token") });
  return { token: r.json("access_token") };
}

export default function (data) {
  const params = (endpoint) => ({ headers: { Authorization: `Bearer ${data.token}`, "Content-Type": "application/json" },
    tags: { kind: "panel", endpoint } });

  const list = http.get(`${BASE}/api/conversations?status=open&limit=50`, params("inbox"));
  check(list, { inbox: (r) => r.status === 200 });
  const convs = list.status === 200 ? list.json() : [];
  if (Array.isArray(convs) && convs.length && Math.random() < 0.8) {
    const c = convs[Math.floor(Math.random() * convs.length)];
    const msgs = http.get(`${BASE}/api/conversations/${c.id}/messages`, params("messages"));
    check(msgs, { messages: (r) => r.status === 200 });
    if (Math.random() < 0.25 && c.status === "human") {
      const send = http.post(`${BASE}/api/conversations/${c.id}/messages`,
        JSON.stringify({ text: "Gracias por escribir, ya te ayudo." }), params("send"));
      check(send, { send: (r) => r.status === 200 || r.status === 409 }); // 409: fuera de la ventana de 24 h
    }
  }
  if (Math.random() < 0.1) {
    const rep = http.get(`${BASE}/api/reports/realtime`, params("reports"));
    check(rep, { reports: (r) => r.status === 200 });
  }
  sleep(1 + Math.random() * 3); // tiempo de lectura del asesor
}
