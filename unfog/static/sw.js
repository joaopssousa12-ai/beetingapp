// Minimal, safe service worker.
// It ONLY caches static assets (cache-first). It never intercepts navigations,
// form POSTs, or any non-static request — those go straight to the network,
// exactly as if no service worker existed. This avoids the classic PWA failure
// where a network-first navigation handler swallows redirects or cold-start
// responses and shows a wrong page.
const CACHE = "unfog-v2";
const STATIC = ["/static/style.css", "/static/app.js", "/static/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      // best-effort: don't fail install if one asset is briefly unavailable
      Promise.allSettled(STATIC.map((u) => c.add(u)))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Only touch same-origin GET requests for static assets. Everything else
  // (pages, form POSTs, redirects) is left entirely to the browser.
  if (
    e.request.method === "GET" &&
    url.origin === self.location.origin &&
    url.pathname.startsWith("/static/")
  ) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});

// Push: show the gentle daily nudge.
self.addEventListener("push", (e) => {
  let d = { title: "Unfog", body: "Your day is waiting 🌱", url: "/app" };
  try { d = Object.assign(d, e.data.json()); } catch (err) {}
  e.waitUntil(
    self.registration.showNotification(d.title, {
      body: d.body,
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      data: { url: d.url },
      tag: "unfog-daily",
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/app";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      for (const c of cs) { if ("focus" in c) { c.navigate(url); return c.focus(); } }
      return self.clients.openWindow(url);
    })
  );
});
