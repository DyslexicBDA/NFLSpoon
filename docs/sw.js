// Minimal offline-caching service worker. No push handling - this app is
// dashboard-only by design (check it yourself, no notifications).
//
// Network-first for everything: every request tries the live site first, and
// only falls back to the last cached copy if you're offline. This is what
// makes edits to the page itself (not just data.json) actually show up on
// your phone without having to reinstall the home-screen icon each time.
const CACHE = 'ff-tracker-v2';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const clone = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, clone));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
