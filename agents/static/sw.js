// OMNAI Inbox service worker minimal
// Cacheia shell estatico, NAO cacheia API (sempre fresh).
const CACHE_NAME = "omnai-inbox-v7";
const SHELL = [
  "/inbox",
  "/static/inbox.html",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/static/favicon-32.png",
  "/manifest.json"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(SHELL)).catch(()=>{}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // API: sempre rede, sem cache
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/actions/") || url.pathname.startsWith("/tasks/")) {
    return; // deixa o browser tratar
  }
  // Shell: cache first, fallback rede
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      if (resp.ok && resp.status === 200) {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      }
      return resp;
    }).catch(() => caches.match("/inbox")))
  );
});
