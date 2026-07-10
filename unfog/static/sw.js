// Minimal service worker: cache static assets, network-first for pages.
const CACHE = "unfog-v1";
const STATIC = ["/static/style.css", "/static/app.js", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
    return;
  }
  e.respondWith(
    fetch(e.request).catch(() =>
      new Response("<h1>Offline</h1><p>Unfog needs a connection for now.</p>", {
        headers: { "Content-Type": "text/html" },
      })
    )
  );
});
