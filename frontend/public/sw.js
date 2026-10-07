/* Service worker de la app del asesor (PWA).
 * - Instalable: precarga el ícono y la página sin conexión.
 * - Navegación: red primero; sin conexión muestra /offline.html (nunca se guardan datos del panel en caché).
 * - Web Push: muestra el aviso y al tocarlo abre (o enfoca) la conversación.
 */
const VERSION = "wa-agent-v1";
const PRECACHE = ["/offline.html", "/icons/icon.svg", "/icons/icon-192.png", "/icons/badge-96.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.mode !== "navigate") return; // la API y los recursos van directo a la red
  event.respondWith(fetch(req).catch(() => caches.match("/offline.html")));
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: "WA Agent", body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "WA Agent";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      requireInteraction: Boolean(data.requireInteraction),
      icon: "/icons/icon-192.png",
      badge: "/icons/badge-96.png",
      data: { url: data.url || "/conversaciones" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/conversaciones", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const same = wins.find((w) => new URL(w.url).origin === self.location.origin);
      if (same) {
        same.focus();
        return "navigate" in same ? same.navigate(url) : undefined;
      }
      return self.clients.openWindow(url);
    }),
  );
});

// El navegador renovó la suscripción: se avisa a la página para que la vuelva a guardar en el servidor
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((wins) => wins.forEach((w) => w.postMessage({ type: "push-resubscribe" }))),
  );
});
