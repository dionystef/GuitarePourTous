/* Guitar Lab — service worker (PWA ready).
   Cache des coquilles (quark) en premier ; les données /data sont récupérées
   depuis le réseau (stems, grilles, tablatures — toujours à jour). */
const CACHE = 'guitarlab-v1';
const SHELL = ['./', '/static/index.html', '/static/style.css', '/static/app.js',
  '/static/icon.svg', '/static/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.pathname.startsWith('/data/') || url.pathname.startsWith('/api/')) {
    // Données : réseau d'abord (fraîcheur), cache en secours hors-ligne.
    e.respondWith(
      fetch(req)
        .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Coquille : cache d'abord.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }))
  );
});